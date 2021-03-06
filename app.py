#!/usr/bin/python3

from flask import Flask, render_template, url_for, redirect, request, g, jsonify, session
from depicts import (utils, wdqs, commons, mediawiki, artwork, database,
                     wd_catalog, human, wikibase, wikidata_oauth, wikidata_edit)
from depicts.pager import Pagination, init_pager
from depicts.model import (DepictsItem, DepictsItemAltLabel, Edit, Item,
                           Language, WikidataQuery, Triple)
from depicts.error_mail import setup_error_mail
from requests_oauthlib import OAuth1Session
from werkzeug.exceptions import InternalServerError
from werkzeug.debug.tbtools import get_current_traceback
from sqlalchemy import func, distinct
from sqlalchemy.orm import aliased
from sqlalchemy.sql.expression import desc
from collections import defaultdict
from datetime import datetime
import itertools
import hashlib
import json
import os
import locale
import random

locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')
user_agent = 'Mozilla/5.0 (X11; Linux i586; rv:32.0) Gecko/20160101 Firefox/32.0'

app = Flask(__name__)
app.config.from_object('config.default')
database.init_db(app.config['DB_URL'])
init_pager(app)
setup_error_mail(app)

find_more_props = {
    'P135': 'movement',
    'P136': 'genre',
    'P170': 'artist',
    'P195': 'collection',
    'P276': 'location',
    'P495': 'country of origin',
    'P127': 'owned by',
    'P179': 'part of the series',
    'P921': 'main subject',
    'P186': 'material used',
    'P88': 'commissioned by',
    'P1028': 'donated by',
    'P1071': 'location of final assembly',
    'P138': 'named after',
    'P1433': 'published in',
    'P144': 'based on',
    'P2079': 'fabrication method',
    'P2348': 'time period',
    'P361': 'part of',
    'P608': 'exhibition history',
    'P180': 'depicts',
    'P31': 'instance of',

    # possible future props
    # 'P571': 'inception',
    # 'P166': 'award received', (only 2)
    # 'P1419': 'shape',  (only 2)
    # 'P123': 'publisher', (only 1)
}

isa_list = [
    'Q60520',     # sketchbook
    'Q93184',     # drawing
    'Q3305213',   # painting
    'Q15123870',  # lithograph
    'Q18761202',  # watercolor painting
    'Q79218',     # triptych
    'Q2647254',   # study
    'Q46686'      # reredos
]

@app.teardown_appcontext
def shutdown_session(exception=None):
    database.session.remove()

@app.errorhandler(InternalServerError)
def exception_handler(e):
    tb = get_current_traceback()
    return render_template('show_error.html', tb=tb), 500

@app.template_global()
def set_url_args(endpoint=None, **new_args):
    if endpoint is None:
        endpoint = request.endpoint
    args = request.view_args.copy()
    args.update(request.args)
    args.update(new_args)
    args = {k: v for k, v in args.items() if v is not None}
    return url_for(endpoint, **args)

@app.template_global()
def current_url():
    args = request.view_args.copy()
    args.update(request.args)
    return url_for(request.endpoint, **args)

@app.before_request
def init_profile():
    g.profiling = []

@app.before_request
def global_user():
    g.user = wikidata_oauth.get_username()

@app.route('/find_more_setting')
def flip_find_more():
    session['no_find_more'] = not session.get('no_find_more')
    display = {True: 'on', False: 'off'}[not session['no_find_more']]

    return 'flipped. find more is ' + display

def existing_edit(item_id, depicts_id):
    q = Edit.query.filter_by(artwork_id=item_id, depicts_id=depicts_id)
    return q.count() != 0

@app.route('/save/Q<int:item_id>', methods=['POST'])
def save(item_id):
    depicts = request.form.getlist('depicts')
    username = wikidata_oauth.get_username()
    assert username

    token = wikidata_oauth.get_token()

    artwork_item = Item.query.get(item_id)
    if artwork_item is None:
        artwork_entity = mediawiki.get_entity_with_cache(f'Q{item_id}')
        label = wikibase.get_entity_label(artwork_entity)
        artwork_item = Item(item_id=item_id, label=label, entity=artwork_entity)
        database.session.add(artwork_item)
        database.session.commit()

    for depicts_qid in depicts:
        depicts_id = int(depicts_qid[1:])

        depicts_item = DepictsItem.query.get(depicts_id)
        if depicts_item is None:
            depicts_item = wikidata_edit.create_depicts_item(depicts_id)
            database.session.add(depicts_item)
            database.session.commit()

    for depicts_qid in depicts:
        depicts_id = int(depicts_qid[1:])
        if existing_edit(item_id, depicts_id):
            continue

        r = create_claim(item_id, depicts_id, token)
        reply = r.json()
        if 'error' in reply:
            return 'error:' + r.text
        print(r.text)
        saved = r.json()
        lastrevid = saved['pageinfo']['lastrevid']
        assert saved['success'] == 1
        edit = Edit(username=username,
                    artwork_id=item_id,
                    depicts_id=depicts_id,
                    lastrevid=lastrevid)
        database.session.add(edit)
        database.session.commit()

    return redirect(url_for('next_page', item_id=item_id))

@app.route('/settings', methods=['GET', 'POST'])
def user_settings():
    return render_template('user_settings.html')

@app.route('/test/lookup')
def test_lookup_page():
    return render_template('test_lookup.html')

@app.route("/property/P<int:property_id>")
def property_query_page(property_id):
    pid = f'P{property_id}'
    g.title = find_more_props[pid]
    sort = request.args.get('sort')
    sort_by_name = sort and sort.lower().strip() == 'name'

    q = (database.session.query(Triple.object_id,
                                func.count(func.distinct(Triple.subject_id)).label('c'))
                         .filter_by(predicate_id=property_id)
                         .join(Item, Item.item_id == Triple.subject_id)
                         .filter_by(is_artwork=True)
                         .group_by(Triple.object_id)
                         .order_by(desc('c')))

    page = utils.get_int_arg('page') or 1
    total = q.count()
    page_size = 100
    pager = Pagination(page, page_size, total)

    page_hits = pager.slice(q)

    labels = get_labels_db({f'Q{object_id}' for object_id, c in page_hits})

    hits = []
    for object_id, count in page_hits:
        qid = f'Q{object_id}'
        hits.append({'qid': qid,
                     'label': labels.get(qid) or '[item missing]',
                     'count': count})

    return render_template('property.html',
                           label=g.title,
                           order=('name' if sort_by_name else 'count'),
                           pid=pid,
                           page=page,
                           pager=pager,
                           hits=hits)

@app.route('/')
def start():
    return random_artwork()

@app.route('/next')
def random_artwork():
    found = None
    while True:
        q = Item.query.filter_by(is_artwork=True).order_by(func.random()).limit(30)
        for item in q:
            has_depicts = 'P180' in item.entity['claims']
            if has_depicts:
                continue
            found = item
            break
        if found:
            break

    session[found.qid] = 'from redirect'
    return redirect(url_for('item_page', item_id=found.item_id))

@app.route('/oauth/start')
def start_oauth():
    next_page = request.args.get('next')
    if next_page:
        session['after_login'] = next_page

    client_key = app.config['CLIENT_KEY']
    client_secret = app.config['CLIENT_SECRET']
    base_url = 'https://www.wikidata.org/w/index.php'
    request_token_url = base_url + '?title=Special%3aOAuth%2finitiate'

    oauth = OAuth1Session(client_key,
                          client_secret=client_secret,
                          callback_uri='oob')
    fetch_response = oauth.fetch_request_token(request_token_url)

    session['owner_key'] = fetch_response.get('oauth_token')
    session['owner_secret'] = fetch_response.get('oauth_token_secret')

    base_authorization_url = 'https://www.wikidata.org/wiki/Special:OAuth/authorize'
    authorization_url = oauth.authorization_url(base_authorization_url,
                                                oauth_consumer_key=client_key)
    return redirect(authorization_url)

@app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    base_url = 'https://www.wikidata.org/w/index.php'
    client_key = app.config['CLIENT_KEY']
    client_secret = app.config['CLIENT_SECRET']

    oauth = OAuth1Session(client_key,
                          client_secret=client_secret,
                          resource_owner_key=session['owner_key'],
                          resource_owner_secret=session['owner_secret'])

    oauth_response = oauth.parse_authorization_response(request.url)
    verifier = oauth_response.get('oauth_verifier')
    access_token_url = base_url + '?title=Special%3aOAuth%2ftoken'
    oauth = OAuth1Session(client_key,
                          client_secret=client_secret,
                          resource_owner_key=session['owner_key'],
                          resource_owner_secret=session['owner_secret'],
                          verifier=verifier)

    oauth_tokens = oauth.fetch_access_token(access_token_url)
    session['owner_key'] = oauth_tokens.get('oauth_token')
    session['owner_secret'] = oauth_tokens.get('oauth_token_secret')

    next_page = session.get('after_login')
    return redirect(next_page) if next_page else random_artwork()

@app.route('/oauth/disconnect')
def oauth_disconnect():
    for key in 'owner_key', 'owner_secret', 'username', 'after_login':
        if key in session:
            del session[key]
    return redirect(url_for('browse_page'))

def create_claim(artwork_id, depicts_id, token):
    artwork_qid = f'Q{artwork_id}'
    value = json.dumps({'entity-type': 'item',
                        'numeric-id': depicts_id})
    params = {
        'action': 'wbcreateclaim',
        'entity': artwork_qid,
        'property': 'P180',
        'snaktype': 'value',
        'value': value,
        'token': token,
        'format': 'json',
        'formatversion': 2,
    }
    return wikidata_oauth.api_post_request(params)

def image_with_cache(qid, image_filename, width):
    filename = f'cache/{qid}_{width}_image.json'
    if os.path.exists(filename):
        detail = json.load(open(filename))
    else:
        detail = commons.image_detail([image_filename], thumbwidth=width)
        json.dump(detail, open(filename, 'w'), indent=2)

    return detail[image_filename]

def existing_depicts_from_entity(entity):
    if 'P180' not in entity['claims']:
        return []
    existing = []
    new_depicts = False
    for claim in entity['claims']['P180']:
        item_id = claim['mainsnak']['datavalue']['value']['numeric-id']

        item = DepictsItem.query.get(item_id)
        if not item:
            item = wikidata_edit.create_depicts_item(item_id)
            database.session.add(item)
            new_depicts = True
        d = {
            'label': item.label,
            'description': item.description,
            'qid': f'Q{item.item_id}',
            'count': item.count,
            'existing': True,
        }
        existing.append(d)
    if new_depicts:
        database.session.commit()
    return existing

def get_institution(entity, other):
    if 'P276' in entity['claims']:
        location = wikibase.first_datavalue(entity, 'P276')
        if location:
            return other[location['id']]
    if 'P195' in entity['claims']:
        collection = wikibase.first_datavalue(entity, 'P195')
        if collection:
            return other[collection['id']]

@app.route("/item/Q<int:item_id>")
def item_page(item_id):
    qid = f'Q{item_id}'
    item = artwork.Artwork(qid)
    from_redirect = qid in session and session.pop(qid) == 'from redirect'
    entity = mediawiki.get_entity_with_cache(qid, refresh=not from_redirect)

    existing_depicts = existing_depicts_from_entity(entity)

    width = 800
    image_filename = item.image_filename
    if image_filename:
        image = image_with_cache(qid, image_filename, width)
    else:
        image = None

    # hits = item.run_query()
    label_and_language = get_entity_label_and_language(entity)
    if label_and_language:
        label = label_and_language['label']
    else:
        label = None
    other = get_other(item.entity)

    people = human.from_name(label) if label else None

    label_languages = label_and_language['languages'] if label_and_language else []
    show_translation_links = all(lang.code != 'en' for lang in label_languages)

    artwork_item = Item.query.get(item_id)
    if artwork_item is None:

        if not wdqs.is_artificial_physical_object(qid):
            return render_template('not_artwork.html',
                           qid=qid,
                           item_id=item_id,
                           item=item,
                           labels=find_more_props,
                           entity=item.entity,
                           username=g.user,
                           label=label,
                           label_languages=label_languages,
                           show_translation_links=show_translation_links,
                           image=image,
                           other=other,
                           title=item.display_title)

        modified = datetime.strptime(entity['modified'], "%Y-%m-%dT%H:%M:%SZ")

        artwork_item = Item(item_id=item_id,
                            entity=entity,
                            lastrevid=entity['lastrevid'],
                            modified=modified)
        database.session.add(artwork_item)

    catalog = wd_catalog.get_catalog_from_artwork(entity)
    if not catalog.get('institution'):
        catalog['institution'] = get_institution(entity, other)

    return render_template('item.html',
                           qid=qid,
                           item_id=item_id,
                           item=item,
                           catalog=catalog,
                           labels=find_more_props,
                           entity=item.entity,
                           username=g.user,
                           label=label,
                           label_languages=label_languages,
                           show_translation_links=show_translation_links,
                           existing_depicts=existing_depicts,
                           image=image,
                           people=people,
                           other=other,
                           # hits=hits,
                           title=item.display_title)

def get_languages(codes):
    return Language.query.filter(Language.wikimedia_language_code.in_(codes))

def get_entity_label_and_language(entity):
    '''
    Look for a useful label and return it with a list of languages that have that label.

    If the entity has a label in English return it.

    Otherwise check if all languages have the same label, if so then return it.
    '''

    group_by_label = defaultdict(set)
    for language, l in entity['labels'].items():
        group_by_label[l['value']].add(language)

    if 'en' in entity['labels']:
        label = entity['labels']['en']['value']
        return {'label': label,
                'languages': get_languages(group_by_label[label])}

    if len(group_by_label) == 1:
        label, languages = list(group_by_label.items())[0]
        return {'label': label,
                'languages': get_languages(languages)}

def get_labels(keys, name=None):
    keys = sorted(keys, key=lambda i: int(i[1:]))
    if name is None:
        name = hashlib.md5('_'.join(keys).encode('utf-8')).hexdigest()
    filename = f'cache/{name}_labels.json'
    labels = []
    if os.path.exists(filename):
        from_cache = json.load(open(filename))
        if isinstance(from_cache, dict) and from_cache.get('keys') == keys:
            labels = from_cache['labels']
    if not labels:
        print(len(keys))
        for num, cur in enumerate(utils.chunk(keys, 50)):
            print(f'{num * 50} / {len(keys)}')
            labels += mediawiki.get_entities(cur, props='labels')

        json.dump({'keys': keys, 'labels': labels},
                  open(filename, 'w'), indent=2)

    return {entity['id']: wikibase.get_entity_label(entity) for entity in labels}

def get_labels_db(keys):
    keys = set(keys)
    labels = {}
    missing = set()
    for qid in keys:
        item = Item.query.get(qid[1:])
        if item:
            labels[qid] = item.label
        else:
            missing.add(qid)

    print(len(missing))
    page_size = 50
    for num, cur in enumerate(utils.chunk(missing, page_size)):
        print(f'{num * page_size} / {len(missing)}')
        for entity in mediawiki.get_entities(cur):
            if 'redirects' in entity:
                continue

            qid = entity['id']

            modified = datetime.strptime(entity['modified'], "%Y-%m-%dT%H:%M:%SZ")
            # FIXME: check if the item is an artwork and set is_artwork correctly
            item = Item(item_id=qid[1:],
                        entity=entity,
                        lastrevid=entity['lastrevid'],
                        modified=modified,
                        is_artwork=False)
            database.session.add(item)
            labels[qid] = item.label
        database.session.commit()

    return labels

def build_other_set(entity):
    other_items = set()
    for key in find_more_props.keys():
        if key not in entity['claims']:
            continue
        for claim in entity['claims'][key]:
            if 'datavalue' in claim['mainsnak']:
                other_items.add(claim['mainsnak']['datavalue']['value']['id'])
    return other_items

def get_other(entity):
    other_items = build_other_set(entity)
    return get_labels(other_items)

@app.route("/edits")
def list_edits():
    q = Edit.query.order_by(Edit.timestamp.desc())
    page = utils.get_int_arg('page') or 1
    pager = Pagination(page, 100, q.count())

    item_count = (database.session
                          .query(func.count(distinct(Edit.artwork_id)))
                          .scalar())

    user_count = (database.session
                          .query(func.count(distinct(Edit.username)))
                          .scalar())

    return render_template('list_edits.html',
                           pager=pager,
                           edit_list=pager.slice(q),
                           item_count=item_count,
                           user_count=user_count)

@app.route("/user/<username>")
def user_page(username):
    edit_list = (Edit.query.filter_by(username=username)
                           .order_by(Edit.timestamp.desc()))

    item_count = (database.session
                          .query(func.count(distinct(Edit.artwork_id)))
                          .filter_by(username=username)
                          .scalar())

    return render_template('user_page.html',
                           username=username,
                           edits=Edit.query,
                           edit_list=edit_list,
                           item_count=item_count)

@app.route("/next/Q<int:item_id>")
def next_page(item_id):
    qid = f'Q{item_id}'

    entity = mediawiki.get_entity_with_cache(qid)

    width = 800
    image_filename = wikibase.first_datavalue(entity, 'P18')
    image = image_with_cache(qid, image_filename, width)

    label = wikibase.get_entity_label(entity)
    other = get_other(entity)

    other_list = []
    for key, prop_label in find_more_props.items():
        if key == 'P186':  # skip material used
            continue       # too generic
        claims = entity['claims'].get(key)
        if not claims:
            continue

        values = []

        for claim in claims:
            if 'datavalue' not in claim['mainsnak']:
                continue
            value = claim['mainsnak']['datavalue']['value']
            claim_qid = value['id']
            if claim_qid == 'Q4233718':
                continue  # anonymous artist
            numeric_id = value['numeric-id']
            href = url_for('find_more_page', property_id=key[1:], item_id=numeric_id)
            values.append({
                'href': href,
                'qid': claim_qid,
                'label': other.get(claim_qid),
            })

        if not values:
            continue

        qid_list = [v['qid'] for v in values]

        other_list.append({
            'label': prop_label,
            'image_lookup': url_for('find_more_json', pid=key, qid=qid_list),
            'pid': key,
            'values': values,
            'images': [],
        })

    return render_template('next.html',
                           qid=qid,
                           label=label,
                           image=image,
                           labels=find_more_props,
                           other=other,
                           entity=entity,
                           other_props=other_list)

@app.route('/P<int:property_id>/Q<int:item_id>')
def find_more_page(property_id, item_id):
    pid, qid = f'P{property_id}', f'Q{item_id}'
    return redirect(url_for('browse_page', **{pid: qid}))

@app.route('/toolinfo.json')
def tool_info():
    info = {
        'name': 'wade',
        'title': 'Wikidata Art Depiction Explorer',
        'description': 'Add depicts statements to works of art.',
        'url': 'https://art.wikidata.link/',
        'keywords': 'art, depicts, paintings, depiction',
        'author': 'Edward Betts',
        'repository': 'https://github.com/edwardbetts/depicts.git',
    }
    return jsonify(info)

def get_facets(params):
    properties = [pid for pid in find_more_props.keys()
                  if pid not in request.args]

    bindings = wdqs.run_from_template_with_cache('query/facet.sparql',
                                                 params=params,
                                                 isa_list=isa_list,
                                                 properties=properties)

    facets = {key: [] for key in find_more_props.keys()}
    for row in bindings:
        pid = row['property']['value'].rpartition('/')[2]
        qid = row['object']['value'].rpartition('/')[2]
        label = row['objectLabel']['value']
        count = int(row['count']['value'])

        if pid not in find_more_props:
            continue
        facets[pid].append({'qid': qid, 'label': label, 'count': count})

    return {
        key: sorted(values, key=lambda i: i['count'], reverse=True)[:15]
        for key, values in facets.items()
        if values
    }

def get_artwork_params():
    return [(pid, qid) for pid, qid in request.args.items()
            if pid.startswith('P') and qid.startswith('Q')]

def filter_artwork(params):
    return wdqs.run_from_template_with_cache('query/find_more.sparql',
                                             params=params,
                                             isa_list=isa_list)

@app.route('/catalog')
def catalog_page():
    params = get_artwork_params()
    bindings = filter_artwork(params)
    page = utils.get_int_arg('page') or 1
    page_size = 45

    item_ids = set()
    for row in bindings:
        item_id = wdqs.row_id(row)
        item_ids.add(item_id)

    qids = [f'Q{item_id}' for item_id in sorted(item_ids)]

    entities = mediawiki.get_entities_with_cache(qids)

    items = []
    other_items = set()
    for entity in entities:
        other_items.update(build_other_set(entity))
        item = {
            'label': wikibase.get_entity_label(entity),
            'qid': entity['id'],
            'item_id': int(entity['id'][1:]),
            'image_filename': wikibase.first_datavalue(entity, 'P18'),
            'entity': entity,
        }
        items.append(item)

    other = get_labels(other_items)

    flat = '_'.join(f'{pid}={qid}' for pid, qid in params)
    thumbwidth = 400
    # FIXME cache_name can be too long for filesystem
    cache_name = f'{flat}_{page}_{page_size}_{thumbwidth}'
    detail = get_image_detail_with_cache(items, cache_name, thumbwidth=thumbwidth)

    for item in items:
        item['url'] = url_for('item_page', item_id=item['item_id'])
        item['image'] = detail[item['image_filename']]

    item_labels = get_labels(qid for pid, qid in params)
    title = ' / '.join(find_more_props[pid] + ': ' + item_labels[qid]
                       for pid, qid in params)

    return render_template('catalog.html',
                           labels=find_more_props,
                           items=items,
                           other=other,
                           title=title)

def get_image_detail_with_cache(items, cache_name, thumbwidth=None, refresh=False):
    filenames = [cur.image_filename() for cur in items]

    if thumbwidth is None:
        thumbwidth = app.config['THUMBWIDTH']

    filename = f'cache/{cache_name}_images.json'
    if not refresh and os.path.exists(filename):
        detail = json.load(open(filename))
    else:
        detail = commons.image_detail(filenames, thumbwidth=thumbwidth)
        json.dump(detail, open(filename, 'w'), indent=2)

    return detail

def browse_index():
    q = (database.session.query(Triple.predicate_id,
                                func.count(func.distinct(Triple.object_id)))
                         .join(Item, Triple.subject_id == Item.item_id)
                         .filter_by(is_artwork=True)
                         .group_by(Triple.predicate_id))

    counts = {f'P{predicate_id}': count for predicate_id, count in q}

    return render_template('browse_index.html',
                           props=find_more_props,
                           counts=counts)

@app.route('/debug/show_user')
def debug_show_user():
    userinfo = wikidata_oauth.userinfo_call()
    return '<pre>' + json.dumps(userinfo, indent=2) + '</pre>'

@app.route('/browse/facets.json')
def browse_facets():
    params = get_artwork_params()
    if not params:
        return jsonify(notice='facet criteria missing')

    facets = get_facets(params)

    for key, values in facets.items():
        for v in values:
            v['href'] = set_url_args(endpoint='browse_page', **{key: v['qid']})

    return jsonify(params=params,
                   facets=facets,
                   prop_labels=find_more_props)

def get_db_items(params):
    ''' Get items for browse page based on criteria. '''
    q = Item.query.filter_by(is_artwork=True)
    for pid, qid in params:
        q = (q.join(Triple, Item.item_id == Triple.subject_id, aliased=True)
              .filter(Triple.predicate_id == pid[1:], Triple.object_id == qid[1:]))

    return q

def get_db_facets(params):
    t = aliased(Triple)
    q = database.session.query(t.predicate_id, func.count().label('count'), t.object_id)
    facet_limit = 18

    for pid, qid in params:
        q = (q.join(Triple, t.subject_id == Triple.subject_id, aliased=True)
              .filter(Triple.predicate_id == pid[1:],
                      Triple.object_id == qid[1:],
                      t.predicate_id != pid[1:],
                      t.object_id != qid[1:]))

    q = q.group_by(t.predicate_id, t.object_id)

    results = sorted(tuple(row) for row in q.all())

    facet_list = {}
    subject_qids = set()
    for predicate_id, x in itertools.groupby(results, lambda row: row[0]):
        hits = sorted(list(x), key=lambda row: row[1], reverse=True)
        values = [{'count': count, 'qid': f'Q{value}'}
                  for _, count, value in hits[:facet_limit]]
        facet_list[f'P{predicate_id}'] = values
        subject_qids.update(i['qid'] for i in values)

    print(len(subject_qids))
    labels = get_labels_db(subject_qids)

    for values in facet_list.values():
        for v in values:
            v['label'] = labels[v['qid']]

    return facet_list

@app.route('/browse')
def browse_page():
    page_size = 45
    params = get_artwork_params()

    if not params:
        return browse_index()

    flat = '_'.join(f'{pid}={qid}' for pid, qid in params)
    item_labels = get_labels_db(qid for pid, qid in params)
    g.title = ' / '.join(find_more_props[pid] + ': ' + (item_labels.get(qid) or qid)
                         for pid, qid in params)

    q_items = get_db_items(params)
    facets = get_db_facets(params)

    all_items = q_items.all()

    page = utils.get_int_arg('page') or 1
    total = q_items.count()
    pager = Pagination(page, page_size, total)

    items = [item for item in pager.slice(all_items) if item.image_filename()]

    cache_name = f'{flat}_{page}_{page_size}'
    detail = get_image_detail_with_cache(items, cache_name)
    cache_refreshed = False

    linked_qids = {qid for pid, qid in params}
    for item in items:
        artist_qid = item.artist
        if artist_qid:
            linked_qids.add(artist_qid)
        for prop in 'P31', 'P180':
            linked_qids.update(item.linked_qids(prop))

    linked_labels = get_labels_db(linked_qids)

    for item in items:
        image_filename = item.image_filename()
        if not cache_refreshed and image_filename not in detail:
            detail = get_image_detail_with_cache(items, cache_name, refresh=True)
            cache_refreshed = True
        item.image = detail[image_filename]

    return render_template('find_more.html',
                           page=page,
                           label=g.title,
                           pager=pager,
                           prop_labels=find_more_props,
                           labels=find_more_props,
                           linked_labels=linked_labels,
                           items=items,
                           total=total,
                           params=params,
                           facets=facets)

    return jsonify(params=params,
                   items=items.count(),
                   facets=facets)

@app.route('/find_more.json')
def find_more_json():
    pid = request.args.get('pid')
    qid_list = request.args.getlist('qid')
    limit = 6

    filenames = []
    cache_name = f'{pid}={",".join(qid_list)}_{limit}'
    bindings = wdqs.run_from_template_with_cache('query/find_more_basic.sparql',
                                                 cache_name=cache_name,
                                                 qid_list=qid_list,
                                                 pid=pid,
                                                 limit=limit)

    items = []
    for row in bindings:
        item_id = wdqs.row_id(row)
        row_qid = f'Q{item_id}'
        image_filename = wdqs.commons_uri_to_filename(row['image']['value'])
        filenames.append(image_filename)
        items.append({'qid': row_qid,
                      'item_id': item_id,
                      'href': url_for('item_page', item_id=item_id),
                      'filename': image_filename})

    thumbheight = 120
    detail = commons.image_detail(filenames, thumbheight=thumbheight)

    for item in items:
        item['image'] = detail[item['filename']]

    return jsonify(items=items)

def wikibase_search(terms):
    hits = []
    r = mediawiki.api_call({
        'action': 'wbsearchentities',
        'search': terms,
        'limit': 'max',
        'language': 'en'
    })
    for result in r.json()['search']:
        hit = {
            'label': result['label'],
            'description': result.get('description') or None,
            'qid': result['id'],
            'count': 0,
        }
        if result['match']['type'] == 'alias':
            hit['alt_label'] = result['match']['text']
        hits.append(hit)

    return hits

def add_images_to_depicts_lookup(hits):
    qid_to_item = {hit['qid']: hit for hit in hits}
    all_qids = [hit['qid'] for hit in hits]
    entities = mediawiki.get_entities_with_cache(all_qids)

    for entity in entities:
        qid = entity['id']
        item = qid_to_item[qid]
        item.entity = entity
    database.session.commit()

    for hit in hits:
        item = qid_to_item[hit['qid']]
        if item.entity:
            image_filename = wikibase.first_datavalue(item.entity, 'P18')
            hit['image_filename'] = image_filename

    filenames = [hit['image_filename']
                 for hit in hits
                 if hit.get('image_filename')]
    filenames = filenames[:50]
    thumbwidth = 200
    detail = commons.image_detail(filenames, thumbwidth=thumbwidth)

    for hit in hits:
        filename = hit.get('image_filename')
        if not filename or filename not in detail:
            continue
        hit['image'] = detail[filename]

@app.route('/lookup')
def depicts_lookup():
    terms = request.args.get('terms')
    if not terms:
        return jsonify(error='terms parameter is required')

    terms = terms.strip()
    if len(terms) < 3:
        return jsonify(
            count=0,
            hits=[],
            notice='terms too short for lookup',
        )

    item_ids = []
    hits = []
    q1 = DepictsItem.query.filter(DepictsItem.label.ilike(terms + '%'))
    seen = set()
    for item in q1:
        hit = {
            'label': item.label,
            'description': item.description,
            'qid': item.qid,
            'count': item.count,
        }
        item_ids.append(item.item_id)
        hits.append(hit)
        seen.add(item.qid)

    cls = DepictsItemAltLabel
    q2 = cls.query.filter(cls.alt_label.ilike(terms + '%'),
                          ~cls.item_id.in_(item_ids))

    for alt in q2:
        item = alt.item
        hit = {
            'label': item.label,
            'description': item.description,
            'qid': item.qid,
            'count': item.count,
            'alt_label': alt.alt_label,
        }
        hits.append(hit)
        seen.add(item.qid)

    hits.sort(key=lambda hit: hit['count'], reverse=True)

    if app.config.get('LOOKUP_INCLUDES_IMAGES'):
        add_images_to_depicts_lookup(hits)

    if app.config.get('SEARCH_WIKIDATA'):
        search_hits = wikibase_search(terms)
        hits += [hit for hit in search_hits if hit['qid'] not in seen]

    ret = {
        'count': q1.count() + q2.count(),
        'hits': hits,
        'terms': terms,
    }

    return jsonify(ret)

@app.route('/report/missing_image')
def missing_image_report():
    limit = utils.get_int_arg('limit') or 1000
    q = DepictsItem.query.order_by(DepictsItem.count.desc()).limit(limit)

    qids = [item.qid for item in q]
    entities = mediawiki.get_entities_dict_with_cache(qids)

    item_list = []

    for depicts in q:
        entity = entities[depicts.qid]
        if any(wikibase.first_datavalue(entity, prop) for prop in ('P18', 'P2716')):
            continue
        item_list.append(depicts)

        # TODO: call wikidata search to find images that depict item

    return render_template('missing_image.html', item_list=item_list)

@app.route('/report/wdqs')
def wikidata_query_list():
    q = WikidataQuery.query.order_by(WikidataQuery.start_time.desc())
    return render_template('query_list.html', q=q)


if __name__ == "__main__":
    app.debug = True
    app.run(host='0.0.0.0', debug=True)
