"""Full catalog export. Medical/health wording is deliberately not a filter.

Unknown required facts remain blank and are reported; they are never invented.
Only structurally excluded records (inactive, missing original SKU, wrong brand)
are omitted from the common workbook.
"""
from __future__ import annotations
import copy, datetime, json
from pathlib import Path
from urllib.parse import urlsplit
import revita_core as core
from screening import ECO, INGREDIENT_HOLD, detect_origins, hits
from listing_policy import ENVIRONMENTAL, factual_text, neutral_title


def evaluate(product, template, config, reviewer):
    from strict_export import columns, profile_for, make_row
    p = copy.deepcopy(product)
    audit = dict(id=p['id'], sku=p.get('reference', ''), url=p['url'],
                 brand=p['brand'], decision='excluded', reasons=[], pending_fields=[],
                 origin='', origin_evidence=[], medical_claim_filter=False,
                 visual_review_required=True)
    images = []
    def exclude(reason):
        audit['reasons'].append(reason)
        return None, audit, images
    if p.get('active') is False:
        return exclude('inactive_product')
    if not str(p.get('reference', '')).strip():
        return exclude('missing_original_sku')
    profile = profile_for(p['brand'], config)
    if not profile:
        return exclude('brand_outside_requested_scope')
    pending = audit['pending_fields']
    for field in ['ingredients', 'directions']:
        if not p.get(field):
            pending.append('missing_' + field)
    # Ingredient restrictions are a different matter from wording on a label.
    if INGREDIENT_HOLD.search(' '.join(p.get('ingredients', [])) + ' ' + p['title']):
        pending.append('ingredient_requires_policy_review')
    override = config.get('product_overrides', {}).get(p['reference'], {})
    category, reason = core.category_for(p, template.categories, override.get('category'))
    audit.update(category=category, category_reason=reason)
    if 'review' in reason:
        pending.append('category_needs_review')
    allowed = template.options(columns(template, 'Country/Region of Origin')[0], {'E': category})
    origins = detect_origins(p.get('source_text', ''), allowed)
    evidence_path = Path(config.get('origin_evidence_file', 'evidence/origin_sources.json'))
    evidence = json.loads(evidence_path.read_text(encoding='utf-8')) if evidence_path.exists() else {}
    fact = evidence.get(p['reference'], {})
    source_host = urlsplit(fact.get('url', '')).hostname
    source_hosts = profile.get('origin_source_hosts', ['revita.bg', 'www.revita.bg'])
    if (fact.get('country') in allowed and fact.get('product_id') == p['id']
            and fact.get('expected_count') == p.get('count') and fact.get('original_title') == p['title']
            and urlsplit(fact.get('url', '')).scheme == 'https' and source_host in source_hosts):
        age = (datetime.datetime.now(datetime.timezone.utc).date()
               - datetime.date.fromisoformat(fact['verified_at'])).days
        if 0 <= age <= 90:
            origins.append(dict(country=fact['country'], evidence=fact['evidence'], source_url=fact['url']))
    clean = []
    for url in dict.fromkeys(p.get('images', []) + p.get('screening_images', [])):
        r = dict(reviewer.inspect(url))
        r.update(id=p['id'], sku=p['reference'], medical=[], other=[], medical_claim_filter=False)
        r['eco'] = hits(r.get('text', ''), ENVIRONMENTAL)
        if r.get('quality_ok'):
            origins.extend({**o, 'image': url} for o in detect_origins(r.get('text', ''), allowed))
        if r['eco']:
            r['status'] = 'eco'
        elif not r.get('quality_ok'):
            r['status'] = 'unknown'
        else:
            r['status'] = 'pass'
            if url in p.get('images', []):
                clean.append(url)
        images.append(r)
    if not clean:
        pending.append('missing_image_after_eco_screening')
    values = {o['country'] for o in origins}
    audit['origin_evidence'] = origins
    if len(values) == 1:
        audit['origin'] = next(iter(values))
    else:
        pending.append('country_of_origin_not_confirmed' if not values else 'conflicting_country_of_origin')
    if p.get('best_before'):
        try:
            if datetime.date.fromisoformat(p['best_before']) < datetime.datetime.now(datetime.timezone.utc).date():
                pending.append('expired_best_before')
        except ValueError:
            pending.append('invalid_best_before')
            p['best_before'] = ''
    if p.get('stock') == 'unknown':
        pending.append('availability_unknown')
    # Retain supplied names, composition and safety directions, but omit eco copy.
    for field in ['ingredients', 'directions']:
        p[field] = [factual_text(s) for s in p.get(field, []) if not hits(factual_text(s), ECO)]
    p['title'] = factual_text(p['title'])[:250]
    if hits(p['title'], ECO):
        p['title'] = neutral_title(p, category)
    p['images'] = clean
    row, problems = make_row(p, template, config, audit['origin'], profile, category)
    pending.extend(problems)
    audit.update(decision='included', retained_images=len(clean), listing_title=p['title'],
                 pending_fields=sorted(set(pending)), required_data_complete=not pending,
                 logistics_defaults=dict(weight_g=config['default_weight_g'], dimensions_cm=config['default_dimensions_cm']))
    return row, audit, images
