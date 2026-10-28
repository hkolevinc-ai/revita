"""Factual listings: clean retailer copy, evaluate actual packaging separately."""
from __future__ import annotations
import copy,json,re,datetime
from pathlib import Path
import revita_core as core
from screening import MEDICAL,OTHER,ECO,hits,detect_origins,SAFETY_PREFIX,INGREDIENT_HOLD

TITLES={'17613':'Магнезий','17630':'Коензим Q10','17706':'Мио-инозитол','17651':'Креатин монохидрат',
        '17655':'L-глутамин','17661':'Таурин','17650':'Ацетил L-карнитин','17657':'L-цитрулин',
        '17621':'Цинк (пиколинат)','17716':'Холин','17711':'Мултивитамини','17604':'Витамини и минерали',
        '17615':'Минерален комплекс','17705':'Витамин B-комплекс','17660':'Бета-аланин'}
HARD_MEDICAL={k:v for k,v in MEDICAL.items() if k!='health_benefit'}
ENVIRONMENTAL={'environmental':ECO['environmental'],'organic':r'\b(?:organic|usda\s+organic|biodynamic)\b|\bбио\b|биоразград\w*|екологич\w*'}

def factual_text(text):
    # Remove optional quality/marketing qualifiers, not nutrient amounts or safety instructions.
    text=re.sub(r'(?i)\b(?:non[ -]?gmo|gmo[ -]?free|organic|certified\s+organic)\b','',str(text))
    text=re.sub(r'(?i)(?:без\s+гмо|не[ -]?гмо|\bбио\b|стандартизиран\s+органичен|\bорганичен\b)', '',text)
    return re.sub(r'\s+',' ',text).strip()

def label_text(text):
    text=re.sub(r'(?is)these\s+statements\s+have\s+not\s+been\s+evaluated\s+by\s+the\s+(?:food\s+and\s+drug\s+administration|fda)\.?','',text)
    text=re.sub(r'(?is)this\s+product\s+is\s+not\s+intended\s+to\s+diagnose.{0,100}?prevent\s+(?:any\s+)?disease\.?','',text)
    return '\n'.join(line for line in text.splitlines() if not SAFETY_PREFIX.search(line.strip()))

def neutral_title(p,category):
    base=TITLES.get(category)
    if not base:
        base=factual_text(p['ingredients'][0])
        base=re.split(r'\d+(?:[.,]\d+)?\s*(?:mg|mcg|µg|μg|g|ml|IU)\b',base,flags=re.I)[0].strip(' ,;:')
    if category=='17613':
        forms=[('бисглицинат','бисглицинат'),('цитрат','цитрат'),('хлорид','хлорид'),('малат','малат'),('таурат','таурат'),('треонат','L-треонат')]
        for token,name in forms:
            if token in p['title'].lower():base+=' ('+name+')';break
    if category=='17630' and 'липозом' in p['title'].lower():base='Липозомен коензим Q10'
    dose=re.search(r'\d+(?:[.,]\d+)?\s*(?:mg|mcg|µg|μg|IU)\b',p['title'],re.I)
    if dose and p['form']!='Gummies':base+=', '+dose.group(0)
    if p.get('count'):
        unit={'Capsules':'капсули','Tablets':'таблетки','Gummies':'желирани таблетки'}.get(p['form'],'броя')
        base+=', '+str(p['count'])+' '+unit
    elif p.get('net') is not None:base+=', '+str(p['net'])+' '+p.get('net_unit','')
    return base[:250]

def evaluate(product,t,config,reviewer):
    from strict_export import columns,profile_for,make_row
    p=copy.deepcopy(product)
    audit={'id':p['id'],'sku':p.get('reference',''),'url':p['url'],'brand':p['brand'],'decision':'excluded','reasons':[],
           'origin':'','origin_evidence':[],'removed_retailer_claims':[],'original_title':p['title'],'visual_review_required':True}
    images=[]
    def stop(reason):audit['reasons'].append(reason);return None,audit,images
    if p.get('active') is False:return stop('inactive_product')
    if not p.get('reference'):return stop('missing_original_sku')
    profile=profile_for(p['brand'],config)
    if not profile:return stop('brand_outside_requested_scope')
    if not p.get('ingredients') or not p.get('directions'):return stop('missing_composition_or_directions')
    if INGREDIENT_HOLD.search(' '.join(p['ingredients'])) or re.search(r'\b(?:NMN|NMNH|NAD\+?|Dynamine|Theacrine)\b',p['title'],re.I):return stop('ingredient_requires_policy_review')
    category,why=core.category_for(p,t.categories,config.get('product_overrides',{}).get(p['reference'],{}).get('category'))
    audit['category']=category;audit['category_reason']=why
    if 'fallback' in why:return stop('category_needs_review')
    # Ambiguous combinations are retained only if an explicit per-SKU category exists.
    if 'multiple' in why and not config.get('product_overrides',{}).get(p['reference'],{}).get('category'):return stop('category_needs_review')
    countries=t.options(columns(t,'Country/Region of Origin')[0],{'E':category})
    origins=detect_origins(p.get('source_text',''),countries)
    evidence_file=Path(config.get('origin_evidence_file','evidence/origin_sources.json'))
    evidence=json.loads(evidence_file.read_text(encoding='utf-8')) if evidence_file.exists() else {}
    fact=evidence.get(p['reference'],{})
    if (fact.get('country') in countries and fact.get('product_id')==p['id'] and fact.get('expected_count')==p.get('count')
        and fact.get('original_title')==p['title'] and fact.get('url','').startswith('https://doublewoodsupplements.com/products/')):
        date=datetime.date.fromisoformat(fact['verified_at'])
        if (datetime.datetime.now(datetime.timezone.utc).date()-date).days<=90:
            origins.append({'country':fact['country'],'evidence':fact['evidence'],'source_url':fact['url'],'verified_at':fact['verified_at']})
    review_file=Path(config.get('image_review_file','evidence/image_reviews.json'))
    decisions=json.loads(review_file.read_text()) if review_file.exists() else {}
    clean=[];unknown=False;medical=False
    for url in dict.fromkeys(p['images']+p.get('screening_images',[])):
        r=reviewer.inspect(url);r['id']=p['id'];r['sku']=p['reference'];text=label_text(r.get('text',''))
        r['medical']=hits(text,HARD_MEDICAL);r['eco']=hits(text,ENVIRONMENTAL);r['other']=hits(text,OTHER)
        health=hits(text,{'health_benefit':MEDICAL['health_benefit']})
        r['health_claims_for_review']=health
        manual=decisions.get(r.get('sha256'),{})
        if r.get('medical'):r['status']='medical';medical=True
        elif r.get('eco'):r['status']='eco'
        elif r.get('other') or health:r['status']='unverified_health_claim'
        elif r.get('quality_ok') or manual.get('decision')=='include':r['status']='pass'
        else:r['status']='unknown';unknown=True
        if manual.get('decision')=='reject':r['status']='manual_reject'
        if r.get('quality_ok'):
            origins += [{**x,'image':url} for x in detect_origins(r.get('text',''),countries)]
        if r['status']=='pass' and url in p['images']:clean.append(url)
        images.append(r)
    if medical:return stop('medical_claim_on_packaging')
    if unknown:return stop('unreadable_image_requires_review')
    if not clean:return stop('no_claim_free_product_images')
    origin_values={x['country'] for x in origins};audit['origin_evidence']=origins
    if not origin_values:return stop('country_of_origin_not_confirmed')
    if len(origin_values)>1:return stop('conflicting_country_of_origin')
    audit['origin']=next(iter(origin_values))
    if p.get('best_before'):
        try:
            if datetime.date.fromisoformat(p['best_before'])<datetime.datetime.now(datetime.timezone.utc).date():return stop('expired_best_before')
        except ValueError:return stop('invalid_best_before')
    audit['removed_retailer_claims']=hits(p.get('claim_text_source',p.get('source_text','')),MEDICAL)
    p['ingredients']=[factual_text(x) for x in p['ingredients']]
    p['directions']=[factual_text(x) for x in p['directions'] if not x.lower().startswith('приложение:')]
    p['title']=neutral_title(p,category);p['images']=clean
    if hits(p['title'],HARD_MEDICAL):return stop('unable_to_create_factual_title')
    row,problems=make_row(p,t,config,audit['origin'],profile,category)
    if problems:audit['reasons']+=problems;return None,audit,images
    retained_records=[r for r in images if r['url'] in clean]
    audit.update({'decision':'included','retained_images':len(clean),'listing_title':p['title'],
                  'visual_review_required':not all(decisions.get(r.get('sha256'),{}).get('decision')=='include' for r in retained_records),
                  'logistics_defaults':{'weight_g':config['default_weight_g'],'dimensions_cm':config['default_dimensions_cm']}})
    return row,audit,images
