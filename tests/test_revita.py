import copy, io, json, tempfile, unittest, zipfile
from pathlib import Path
from unittest.mock import patch
from lxml import etree
import revita_core as core
import strict_export as export
from screening import hits, MEDICAL, ECO, text_screen, detect_origins, ImageReviewer

ROOT=Path(__file__).resolve().parents[1]

def product(**changes):
    p={'id':'001','reference':'001-A NF','title':'Vitamin B12 1000 mcg, 60 capsules','url':'https://revita.bg/shop/vitamin-b12-test',
       'brand':"Nature's Way",'manufacturer_source':"Nature's Way, USA",'stock':'in','price':12.5,'list_price':15.0,
       'ingredients':['Vitamin B12 (Cobalamin) 1000 mcg'],'directions':['Suggested use: 1 capsule daily.','Warning: do not exceed the recommended daily dose.'],
       'source_text':'Made in USA\nVitamin B12 1000 mcg','form':'Capsules','count':60,'net':None,'net_unit':'','ean':'',
       'images':['https://revita.bg/label.jpg'],'best_before':'','variant_id':'0','variant_label':'','variant_options':{},'active':True}
    p.update(changes);return p

class Images:
    def __init__(self,items=None):self.items=items or {}
    def preflight(self):pass
    def inspect(self,url):
        value={'url':url,'status':'pass','quality_ok':True,'text':'Vitamin B12 Made in USA 60 capsules','medical':[],'eco':[],'other':[]}
        value.update(self.items.get(url,{}));return value

class ScreeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template=core.Template(ROOT/'templates/temu_template.xlsx')
        cls.config=json.loads((ROOT/'config.json').read_text())
    def screen(self,p=None,reviewer=None):
        return export.screen_product(p or product(),self.template,self.config,reviewer or Images())
    def test_original_sku_and_dynamic_new_template_legal_columns(self):
        row,a,_=self.screen();self.assertEqual(a['decision'],'included',a)
        self.assertEqual(row['N'],'001-A NF');self.assertEqual(row['M'],'001-A NF')
        self.assertEqual(row['LW'],'United States')
        self.assertEqual(row['NW'],"Nature's Way Brands, LLC")
        self.assertEqual(row['NX'],'Geo Nutrition LTD.')
        self.assertEqual(row['R'],"NATURE'S WAY");self.assertEqual(row['S'],"Nature's Way")
        self.assertEqual(row['LS'],'Magazin');self.assertEqual(row['LB'],12.5)
        self.assertEqual(row['LN'],60);self.assertEqual(row['LO'],60);self.assertEqual(row['LP'],'count')
    def test_all_three_exact_registered_brands(self):
        for brand,expected in [('Alive','ALIVE!'),('Double Wood','DOUBLE WOOD'),('Nature’s Way',"NATURE'S WAY")]:
            row,a,_=self.screen(product(brand=brand));self.assertIsNotNone(row,a);self.assertEqual(row['R'],expected)
    def test_no_seven_brand_scope(self):
        self.assertEqual(export.CATEGORIES,['alive','double-wood','natures-way'])
        row,a,_=self.screen(product(brand='Natural Factors'));self.assertIsNone(row);self.assertIn('brand_outside_requested_scope',a['reasons'])
    def test_medical_title_and_original_marketing_drop_whole_product(self):
        for change in [{'title':'Treats diabetes 60 capsules'},{'source_text':'Made in USA\nЛекува артрит и болки.'},{'source_text':'Supports heart health.'}]:
            row,a,images=self.screen(product(**change));self.assertIsNone(row);self.assertEqual(images,[]);self.assertIn('medical_or_unsupported_claim_in_product_text',a['reasons'])
    def test_disease_in_mandatory_warning_is_not_a_treatment_claim(self):
        p=product(source_text='Made in USA\nWarning: consult your doctor if you have diabetes.')
        self.assertEqual(text_screen(p),[])
    def test_clinical_and_fda_claims_held(self):
        for text in ['FDA approved','Clinically proven','No side effects']:
            self.assertTrue(text_screen(product(title=text)))
        self.assertTrue(text_screen(product(source_text='',claim_text_source='AI summary: Treats arthritis')))
    def test_environmental_image_removed_from_all_image_columns(self):
        p=product(images=['https://revita.bg/clean.jpg','https://revita.bg/eco.jpg'])
        r=Images({'https://revita.bg/eco.jpg':{'status':'eco','text':'Eco friendly recyclable packaging','eco':[{'rule':'environmental'}]}})
        row,a,images=self.screen(p,r);self.assertIsNotNone(row,a)
        self.assertEqual(len(images),2)
        self.assertNotIn('https://revita.bg/eco.jpg',row.values())
        self.assertEqual(row['KQ'],'https://revita.bg/clean.jpg')
    def test_medical_image_excludes_entire_product(self):
        p=product(images=['https://revita.bg/clean.jpg','https://revita.bg/medical.jpg'])
        r=Images({'https://revita.bg/medical.jpg':{'status':'medical','medical':[{'match':'pain relief'}]}})
        row,a,_=self.screen(p,r);self.assertIsNone(row);self.assertIn('medical_claim_in_product_image',a['reasons'])
        p=product(screening_images=['https://revita.bg/medical.jpg'])
        row,a,_=self.screen(p,r);self.assertIsNone(row);self.assertIn('medical_claim_in_product_image',a['reasons'])
    def test_ocr_failure_never_exports_unreviewed_image(self):
        row,a,_=self.screen(reviewer=Images({'https://revita.bg/label.jpg':{'status':'unknown','error':'OCR failed'}}))
        self.assertIsNone(row);self.assertIn('image_could_not_be_reliably_screened',a['reasons'])
    def test_no_clean_images_excludes_product(self):
        row,a,_=self.screen(reviewer=Images({'https://revita.bg/label.jpg':{'status':'eco'}}))
        self.assertIsNone(row);self.assertIn('no_claim_free_product_images',a['reasons'])
    def test_country_is_not_manufacturer_address(self):
        p=product(source_text="Manufacturer: Nature's Way, USA")
        r=Images({'https://revita.bg/label.jpg':{'text':'Vitamin B12 60 capsules'}})
        row,a,_=self.screen(p,r);self.assertIsNone(row);self.assertIn('country_of_origin_not_confirmed',a['reasons'])
    def test_origin_from_label_and_conflicts(self):
        p=product(source_text='Vitamin B12')
        row,a,_=self.screen(p);self.assertIsNotNone(row,a);self.assertTrue(a['origin_evidence'][0].get('image'))
        row,a,_=self.screen(product(source_text='Made in Canada'))
        self.assertIsNone(row);self.assertIn('conflicting_country_of_origin',a['reasons'])
    def test_various_explicit_origins(self):
        allowed=['United States','Canada','India']
        for text,country in [('Произведено в САЩ','United States'),('Made in U.S.A. with global ingredients','United States'),('Country of origin: India','India')]:
            self.assertEqual(detect_origins(text,allowed)[0]['country'],country)
        self.assertEqual(detect_origins('Distributed by Example, USA',allowed),[])
    def test_eco_paragraph_not_copied_but_ingredients_not_silently_rewritten(self):
        row,a,_=self.screen(product(source_text='Made in USA\nSustainable recyclable packaging.'))
        self.assertIsNotNone(row,a);self.assertEqual(a['removed_eco_paragraphs'],1)
        self.assertNotIn('recyclable',row['T'])
        row,a,_=self.screen(product(ingredients=['Organic ginger extract']))
        self.assertIsNone(row);self.assertIn('ecological_claim_in_ingredients_or_directions',a['reasons'])
    def test_biotin_and_brand_nature_are_not_eco_claims(self):
        self.assertFalse(hits('Biotin 5000 mcg by Nature’s Way',ECO))
        self.assertFalse(hits('Curcumin',MEDICAL))
        self.assertTrue(hits('GMO NON Project VERIFIED',ECO))
        self.assertTrue(hits('G M O free',ECO))
        self.assertTrue(hits('TRADITIONAL TONIC FOR WELL-BEING',MEDICAL))
    def test_missing_or_inactive_original_sku_is_held(self):
        for p,reason in [(product(reference=''),'missing_original_sku'),(product(active=False),'inactive_product')]:
            row,a,_=self.screen(p);self.assertIsNone(row);self.assertIn(reason,a['reasons'])
    def test_no_country_or_sku_override_bypass(self):
        cfg=copy.deepcopy(self.config);cfg['product_overrides']={'001-A NF':{'fields':{'Contribution SKU':'REV-001'}}}
        with self.assertRaises(ValueError):export.make_row(product(),self.template,cfg,'United States',export.profile_for("Nature's Way",cfg),'17703')
    def test_xlsx_native_parts_retained(self):
        row,a,_=self.screen()
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'test.xlsx';self.template.write([row],path)
            with zipfile.ZipFile(path) as z:
                self.assertIsNone(z.testzip())
                for name,body in self.template.parts.items():
                    if name!=self.template.sheet_path:self.assertEqual(z.read(name),body,name)
                root=etree.fromstring(z.read(self.template.sheet_path))
                self.assertEqual(root.find(core.M+'dimension').get('ref'),'A1:NY5')
                self.assertEqual(len(root.find(core.M+'sheetData')),5)
    def test_duplicate_original_skus_are_held_in_full_pipeline(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot=Path(temp)/'snapshot.json';snapshot.write_text(json.dumps([product(),product(id='002')]))
            cfg=copy.deepcopy(self.config);cfg['template']=str(ROOT/'templates/temu_template.xlsx')
            config=Path(temp)/'config.json';config.write_text(json.dumps(cfg))
            output=Path(temp)/'output'
            with patch.object(export,'ImageReviewer',return_value=Images()):
                status=export.main(['--config',str(config),'--from-catalog',str(snapshot),'--output',str(output)])
            s=json.loads((output/'summary.json').read_text());self.assertEqual(status,0)
            self.assertEqual(s['exported_rows'],0);self.assertEqual(s['exclusion_reasons']['duplicate_original_sku'],2)
            self.assertFalse(list(output.glob('*.xlsx')))
    def test_live_pipeline_only_three_brands_keeps_current_skus(self):
        from urllib.parse import urlsplit
        brands={'alive':'Alive','double-wood':'Double Wood','natures-way':"Nature's Way"}
        skus={'alive':'AL-0001','double-wood':'DW 0002','natures-way':'00003 ST'}
        touched=[]
        class Client:
            def __init__(self,*args,**kwargs):self.attempts=[]
            def get(self,url,kind=None,**kwargs):
                touched.append(url);slug=urlsplit(url).path.strip('/').replace('shop/','')
                pid=str(list(brands).index(slug)+1)
                product_url=core.BASE+'/shop/'+slug
                if kind=='listing':
                    return json.dumps({'rendered_products':'<div id="js-product-list"><article class="product-miniature" data-id-product="'+pid+'"><p class="product-title"><a href="'+product_url+'">Vitamin B12</a></p><div class="product-reference"><span>'+skus[slug]+'</span></div></article></div>',
                        'pagination':{'total_items':1,'pages_count':1,'current_page':1},'products':[{'url':product_url,'active':1}]}).encode()
                substance=json.dumps({'@type':'Substance','url':product_url,'activeIngredient':['Vitamin B12 (Cobalamin) 1000 mcg'],'maximumIntake':['Suggested use: 1 capsule daily.']})
                return ('<html><head><meta charset="utf-8"><link rel="canonical" href="'+product_url+'"><script type="application/ld+json">'+substance+'</script></head><body><section id="main"><div id="main-product-wrapper"><h1>Vitamin B12 1000 mcg, 60 capsules</h1><div class="product-manufacturer"><img alt="'+brands[slug]+'"></div><form id="add-to-cart-or-refresh"><input name="id_product" value="'+pid+'"><div class="product-prices"><div class="product-reference"><span>'+skus[slug]+'</span></div><link href="https://schema.org/InStock"><span class="product-price" content="12.50" data-currency_sign="€">12.50 €</span></div></form><div id="product-images-large"><img data-image-large-src="https://revita.bg/label.jpg"></div></div><div class="elementor-text-editor"><p>Made in USA</p></div></section></body></html>').encode()
        with tempfile.TemporaryDirectory() as temp:
            cfg=copy.deepcopy(self.config);cfg['template']=str(ROOT/'templates/temu_template.xlsx')
            config=Path(temp)/'config.json';config.write_text(json.dumps(cfg));output=Path(temp)/'out'
            with patch.object(export.core,'Client',Client),patch.object(export,'ImageReviewer',return_value=Images()):
                status=export.main(['--config',str(config),'--output',str(output),'--refresh'])
            self.assertEqual(status,0)
            summary=json.loads((output/'summary.json').read_text());self.assertEqual(summary['exported_rows'],3)
            self.assertTrue(summary['complete_crawl'])
            included=list(__import__('csv').DictReader(io.StringIO((output/'included.csv').read_text(encoding='utf-8-sig'))))
            self.assertEqual({r['sku'] for r in included},set(skus.values()))
            self.assertTrue((output/'TEMU_REVITA_FILTERED_001.xlsx').exists())
            self.assertTrue(all(urlsplit(u).path.strip('/').replace('shop/','') in brands for u in touched))

class OcrEngineTests(unittest.TestCase):
    def test_missing_engine_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp,patch('screening.shutil.which',return_value=None):
            with self.assertRaises(RuntimeError):ImageReviewer(cache=temp).preflight()
    def test_image_host_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            result=ImageReviewer(cache=temp).inspect('https://example.com/image.jpg')
            self.assertEqual(result['status'],'unknown')
