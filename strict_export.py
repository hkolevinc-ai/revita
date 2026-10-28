"""Three Revita brand pages -> screened Temu template, original merchant SKUs."""
from __future__ import annotations
import argparse, collections, copy, csv, html, json, logging, re
from datetime import datetime, timezone
from pathlib import Path
import revita_core as core
from screening import ImageReviewer, ECO, MEDICAL, OTHER, hits, neutral_description, detect_origins, text_screen, SAFETY_PREFIX

LOG=logging.getLogger('revita.strict')
CATEGORIES=['alive','double-wood','natures-way']

def columns(t, header, prefix=None):
    return [c for c,h in t.headers.items() if h==header and (not prefix or t.codes.get(c,'').startswith(prefix))]

def profile_for(brand, config):
    key=core.norm(brand)
    return next((p for p in config['brands'].values() if key in [core.norm(x) for x in p['aliases']]),None)

def make_row(p,t,config,origin,profile,category):
    row={}
    def put(header,value,prefix=None):
        found=columns(t,header,prefix)
        if found:row[found[0]]=value
    sku=p['reference']
    put('Category',category);put('Category Name',t.categories[category])
    fields={'Product type':'Normal product','Product Name':p['title'],
            'Contribution Goods':p.get('parent_reference') or sku,'Contribution SKU':sku,
            'Update or Add':'Add','Brand':profile['brand'],'Trademark':profile['trademark'],
            'Product Description':neutral_description(p),'Variation Theme':'Size',
            'Size':p.get('variant_label') or 'One size','1055 - Product Dosage Form':p['form'],
            '7926 - Final Dosage Form':'Liquid' if p['form']=='Liquid' else 'Solid' if p['form'] in ['Capsules','Tablets','Gummies','Powder','Pellets','Granules','Tea Bag'] else 'Not Applicable',
            'Base Price - EUR':p['price'],'List Price - EUR':p['list_price'],'Reference Link':p['url'],
            'Weight - g':config['default_weight_g'],'Length - cm':config['default_dimensions_cm'][0],
            'Width - cm':config['default_dimensions_cm'][1],'Height - cm':config['default_dimensions_cm'][2],
            'SKU type':'Single set','Individually packed':'Yes','Total packaging quantity':1,'Packaging unit':'pack',
            'Shipping Template':config['shipping_template'],'Handling Time':config['handling_time'],
            'Fulfillment Channel':'I will ship this item myself','Country/Region of Origin':origin,
            'Manufacturer':profile['manufacturer'],'EU Responsible person':profile['eu_responsible']}
    for h,v in fields.items():put(h,v)
    put('Quantity',config['stock_if_available'] if p['stock']=='in' else 0,'t_6_')
    content=p.get('net') if p.get('net') is not None else p.get('count')
    unit=p.get('net_unit') if p.get('net') is not None else 'count'
    if content is not None:
        put('Net content',content);put('Total net content',content);put('Net content unit',unit)
    if p.get('best_before'):put('6440 - Best before',p['best_before'])
    if re.fullmatch(r'\d{12,14}',p.get('ean','')):
        put('External Product ID Type','UPC' if len(p['ean'])==12 else 'EAN' if len(p['ean'])==13 else 'GTIN-14');put('External Product ID',p['ean'])
    for h in ['Detail Images URL','SKU Images URL']:
        for c,url in zip(columns(t,h),p['images']):row[c]=url
    bullet=['Марка: '+profile['trademark'],'Форма: '+p['form']]
    if p.get('count'):bullet.append('Брой в опаковка: '+str(p['count']))
    for c,v in zip(columns(t,'Bullet Point'),bullet):row[c]=v
    used=set()
    for header,child in [('7171 - Ingredients 1','7174 - Main Ingredient 1'),('7175 - Ingredient 2','7176 - Main Ingredient 2'),('7177 - Ingredient 3','7178 - Main Ingredients 3'),('7179 - Ingredient 4','7181 - Main Ingredients 4'),('7182 - Ingredient 5','7180 - Main Ingredients 5')]:
        for ingredient in p['ingredients']:
            group,sub=core.ingredient_group(ingredient)
            if not group or (group,sub) in used:continue
            col=columns(t,header)[0];selected=t.choose(col,group,row)
            if not selected:continue
            row[col]=selected
            if sub:row[columns(t,child)[0]]=t.choose(columns(t,child)[0],sub,row)
            used.add((group,sub));break
    for header,value in config.get('product_overrides',{}).get(sku,{}).get('fields',{}).items():
        if header in ['Contribution SKU','Contribution Goods','Product Name','Product Description','Brand','Trademark','Manufacturer','EU Responsible person','Country/Region of Origin','Detail Images URL','SKU Images URL']:
            raise ValueError('Protected field override: '+header)
        if not columns(t,header):raise ValueError('Unknown field override: '+header)
        put(header,value,'t_6_' if header=='Quantity' else None)
    problems=[]
    for c,v in list(row.items()):
        selected=t.choose(c,v,row)
        if v not in ('',None) and selected=='':problems.append('dropdown_mismatch:'+t.headers[c])
        row[c]=selected
    for header in ['Brand','Trademark','Manufacturer','EU Responsible person','Country/Region of Origin']:
        if not row.get(columns(t,header)[0]):problems.append('missing_identity:'+header)
    checked=set()
    for c in t.requirements.get(str(category),[]):
        h=t.headers[c]
        if h in checked:continue
        checked.add(h)
        if not any(row.get(k) not in ('',None) for k in columns(t,h)):problems.append('missing_required:'+h)
    for c,v in row.items():
        if t.headers[c] in ['Product Name','Bullet Point','Product Description']:
            for line in str(v).splitlines():
                if hits(line,ECO):problems.append('eco_claim_in_final_text')
                if not SAFETY_PREFIX.search(line.strip()) and (hits(line,MEDICAL) or hits(line,OTHER)):problems.append('claim_in_final_text')
    return row,sorted(set(problems))

def screen_product(p,t,config,reviewer):
    p=copy.deepcopy(p)
    audit={'id':p['id'],'sku':p.get('reference',''),'url':p['url'],'brand':p['brand'],'decision':'excluded','reasons':[],'origin':'','origin_evidence':[], 'removed_eco_paragraphs':0}
    image_rows=[]
    def stop(reason):
        audit['reasons'].append(reason)
        return None,audit,image_rows
    if p.get('active') is False:return stop('inactive_product')
    if not p.get('reference') or not str(p['reference']).strip():return stop('missing_original_sku')
    profile=profile_for(p['brand'],config)
    if not profile:return stop('brand_outside_requested_scope')
    findings=text_screen(p)
    if findings:
        audit['claim_evidence']=findings;return stop('medical_or_unsupported_claim_in_product_text')
    if hits(p['title'],ECO):return stop('ecological_claim_in_product_name')
    if not p.get('ingredients'):return stop('composition_not_found')
    if not p.get('directions'):return stop('directions_not_found')
    if hits('\n'.join(p['ingredients']+p.get('directions',[])),ECO):return stop('ecological_claim_in_ingredients_or_directions')
    if p['stock']=='unknown':return stop('availability_unknown')
    category,why=core.category_for(p,t.categories,config.get('product_overrides',{}).get(p['reference'],{}).get('category'))
    audit['category']=category;audit['category_reason']=why
    if 'fallback' in why or 'multiple' in why:return stop('category_needs_review')
    audit['removed_eco_paragraphs']=sum(bool(hits(x,ECO)) for x in p.get('source_text','').splitlines())
    country_options=t.options(columns(t,'Country/Region of Origin')[0],{'E':category})
    origins=detect_origins(p.get('source_text',''),country_options)
    clean_images=[];medical_images=[];unknown_images=[]
    for url in dict.fromkeys(p['images']+p.get('screening_images',[])):
        r=reviewer.inspect(url);r['id']=p['id'];r['sku']=p['reference'];image_rows.append(r)
        if r.get('quality_ok'):
            origins += [{**match,'image':url} for match in detect_origins(r.get('text',''),country_options)]
        if r['status']=='medical':medical_images.append(url)
        elif r['status']=='unknown':unknown_images.append(url)
        elif r['status']=='pass' and url in p['images']:clean_images.append(url)
    if medical_images:return stop('medical_claim_in_product_image')
    if unknown_images:return stop('image_could_not_be_reliably_screened')
    if not clean_images:return stop('no_claim_free_product_images')
    countries=sorted({x['country'] for x in origins});audit['origin_evidence']=origins
    if not countries:return stop('country_of_origin_not_confirmed')
    if len(countries)>1:return stop('conflicting_country_of_origin')
    audit['origin']=countries[0];p['images']=clean_images
    if len(p['title'])>250:return stop('title_too_long_requires_review')
    row,problems=make_row(p,t,config,countries[0],profile,category)
    if problems:audit['reasons']+=problems;return None,audit,image_rows
    audit.update({'decision':'included','retained_images':len(clean_images),'visual_review_required':True,
                  'logistics_defaults':{'weight_g':config['default_weight_g'],'dimensions_cm':config['default_dimensions_cm']}})
    return row,audit,image_rows

def discover(client):
    links={};coverage=[];errors=[]
    for slug in CATEGORIES:
        start=core.BASE+'/'+slug;queue=[start];visited=set();signatures=set();found=set();total=None
        while queue:
            url=queue.pop(0)
            if url in visited:continue
            visited.add(url)
            try:
                hints,pages,reported=core.parse_listing(client.get(url,kind='listing'),start)
                signature=tuple(sorted(hints))
                if signature in signatures:raise ValueError('Repeated listing page')
                signatures.add(signature)
                if reported is not None:total=reported
                for link,hint in hints.items():
                    found.add(link)
                    if link not in links:links[link]={'hint':hint,'category':slug}
                queue += [page for page in pages if page not in visited and page not in queue]
                LOG.info('%s: page %s, discovered %s',slug,len(visited),len(found))
            except Exception as exc:errors.append({'stage':'category','url':url,'error':str(exc)})
        if total is not None and total!=len(found):errors.append({'stage':'category','url':start,'error':f'Count mismatch {len(found)} vs {total}'})
        coverage.append({'category':slug,'pages':len(visited),'products_found':len(found),'reported_total':total,
                         'status':'ERROR' if any(core.canonical(x['url'])==start for x in errors) else 'OK'})
    return links,coverage,errors

def csv_write(path,rows,fields):
    with Path(path).open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader()
        for row in rows:writer.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()})

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config',default='config.json');ap.add_argument('--limit',type=int,default=0)
    ap.add_argument('--output',default='output');ap.add_argument('--refresh',action='store_true')
    ap.add_argument('--from-catalog',help='Offline snapshot JSON for testing, not current prices')
    args=ap.parse_args(argv)
    if args.limit<0:ap.error('limit must be >= 0')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    config=json.loads(Path(args.config).read_text(encoding='utf-8'));t=core.Template(config['template'])
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    for old in output.glob('TEMU_REVITA_FILTERED_*.xlsx'):old.unlink()
    reviewer=ImageReviewer(**config['ocr'],refresh=args.refresh);reviewer.preflight()
    errors=[];coverage=[];links={};products=[];client=None
    if args.from_catalog:
        products=[p for p in json.loads(Path(args.from_catalog).read_text(encoding='utf-8')) if profile_for(p.get('brand',''),config)]
        if args.limit:products=products[:args.limit]
    else:
        client=core.Client(delay=1.5,refresh=args.refresh,diagnostics=output/'diagnostics')
        links,coverage,errors=discover(client)
        for i,(url,info) in enumerate(list(links.items())[:args.limit or None],1):
            try:
                body=client.get(url,kind='product',expected_id=info['hint'].get('id') or None)
                p=core.parse_product(body,url,info['hint'],info['category']);p['active']=info['hint'].get('active',True)
                p['parent_reference']=p['reference'];products.extend(core.expand_variants(p,body,client))
                LOG.info('Product %s/%s SKU %s',i,len(links),p['reference'])
            except Exception as exc:errors.append({'stage':'product','url':url,'error':str(exc)})
    audits=[];image_audits=[];candidates=[];seen=set()
    for i,p in enumerate(products,1):
        identity=(p['id'],p.get('variant_id','0'))
        if identity in seen:continue
        seen.add(identity)
        try:
            row,audit,images=screen_product(p,t,config,reviewer);audits.append(audit);image_audits+=images
            if row:candidates.append((row,audit))
            LOG.info('Screen %s/%s %s: %s',i,len(products),p['reference'],audit['decision'])
        except Exception as exc:
            errors.append({'stage':'screening','url':p['url'],'error':str(exc)})
            audits.append({'id':p['id'],'sku':p.get('reference',''),'url':p['url'],'decision':'excluded','reasons':['screening_error']})
    unique_products={(p['id'],p.get('variant_id','0')):p for p in products}
    counts=collections.Counter(p.get('reference','') for p in unique_products.values() if p.get('active') is not False);rows=[]
    for row,audit in candidates:
        if counts[audit['sku']]>1:audit['decision']='excluded';audit['reasons'].append('duplicate_original_sku')
        else:rows.append(row)
    for start in range(0,len(rows),4996):t.write(rows[start:start+4996],output/f'TEMU_REVITA_FILTERED_{start//4996+1:03d}.xlsx')
    def save(name,obj):(output/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
    save('catalog.json',products);save('screening.json',audits);save('image_screening.json',image_audits)
    csv_write(output/'excluded.csv',[a for a in audits if a['decision']!='included'],['id','sku','url','brand','reasons','claim_evidence','origin','origin_evidence','category_reason'])
    csv_write(output/'included.csv',[a for a in audits if a['decision']=='included'],['id','sku','url','brand','origin','origin_evidence','retained_images','removed_eco_paragraphs','logistics_defaults','visual_review_required'])
    csv_write(output/'images.csv',image_audits,['id','sku','url','status','sha256','confidence','medical','eco','other','error'])
    csv_write(output/'errors.csv',errors,['stage','url','error'])
    save('coverage.json',coverage);save('discovered_urls.json',links);save('http_attempts.json',getattr(client,'attempts',[]))
    cards=['<article><p>'+html.escape(str(r['sku']))+'</p><img loading="lazy" src="'+html.escape(r['url'],quote=True)+'"><p>'+html.escape(r['url'])+'</p></article>' for r in image_audits if r['status']=='pass']
    (output/'image_review.html').write_text('<!doctype html><meta charset="utf-8"><title>Визуален преглед</title><style>body{font:16px Arial}main{display:flex;flex-wrap:wrap}article{width:340px;margin:12px;overflow-wrap:anywhere}img{width:320px;height:420px;object-fit:contain}</style><h1>Изображения след OCR проверка</h1><p>Проверете значки без текст, дребен печат и оставащи твърдения. OCR не удостоверява пълно визуално съответствие.</p><main>'+''.join(cards)+'</main>',encoding='utf-8')
    summary={'mode':'offline snapshot' if args.from_catalog else 'live','timestamp':datetime.now(timezone.utc).isoformat(),
             'categories':CATEGORIES,'discovered_products':len(links),'scanned_products':len(products),'exported_rows':len(rows),
             'excluded_products':sum(a['decision']!='included' for a in audits),'errors':len(errors),
             'exclusion_reasons':dict(collections.Counter(x for a in audits if a['decision']!='included' for x in a['reasons'])),
             'complete_crawl':not args.limit and not args.from_catalog and not errors and all(x['status']=='OK' for x in coverage),
             'original_skus_preserved':True,'visual_review_required':bool(rows),'temu_approval_guaranteed':False}
    save('summary.json',summary)
    LOG.info('Exported %s; excluded %s; errors %s',len(rows),summary['excluded_products'],len(errors))
    return 1 if errors else 0
