import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from lxml import etree, html
import revita

ROOT = Path(__file__).resolve().parents[1]


class RevitaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.product_source = (ROOT/'tests/fixtures/product.html').read_bytes()
        cls.list_source = (ROOT/'tests/fixtures/category.html').read_bytes()
        cls.url = revita.first(revita.parse_doc(cls.product_source),'//link[@rel="canonical"]/@href')
        cls.hints, cls.pages, _ = revita.parse_listing(cls.list_source,revita.BASE+'/alive')
        cls.product = revita.parse_product(cls.product_source,cls.url,cls.hints[cls.url],'Alive')
        cls.template = revita.Template(ROOT/'templates/temu_template.xlsx')
        cls.config = json.loads((ROOT/'config.json').read_text())

    def test_listing_and_span_pagination(self):
        self.assertEqual(len(self.hints),24)
        self.assertIn('https://revita.bg/alive?page=2',self.pages)
        doc=revita.parse_doc(self.list_source)
        for a in doc.xpath('//nav//a'):a.getparent().remove(a)
        _, pages, _=revita.parse_listing(html.tostring(doc),revita.BASE+'/alive')
        self.assertEqual(pages,['https://revita.bg/alive?page=2'])

    def test_primary_product_exact_values(self):
        p=self.product
        self.assertEqual(p['id'],'586')
        self.assertEqual(p['reference'],'15789')
        self.assertEqual(p['brand'],'Alive')
        self.assertEqual(p['price'],16.61)
        self.assertEqual(p['list_price'],33.21)
        self.assertEqual(p['stock'],'in')
        self.assertEqual(p['form'],'Gummies')
        self.assertEqual(p['count'],90)
        self.assertEqual(len(p['images']),3)
        self.assertTrue(all('thickbox_default' in x for x in p['images']))

    def test_composition_and_directions(self):
        p=self.product
        self.assertIn('Витамин А',p['ingredients'][0])
        self.assertTrue(any('150 µg' in x for x in p['ingredients']))
        self.assertIn('Nature’s Way',p['manufacturer_source'])
        self.assertTrue(any('1 желирана таблетка' in x for x in p['directions']))
        self.assertNotIn('Анелия',p['description'])
        self.assertNotIn('третата сливица',p['description'])

    def test_manufacturer_country_not_assumed_origin(self):
        self.assertIn('USA',self.product['manufacturer_source'])
        self.assertEqual(self.product['country_source'],'')

    def test_category_for_supplied_product(self):
        cat,why=revita.category_for(self.product,self.template.categories)
        self.assertEqual(cat,'17711')
        self.assertEqual(why,'title')

    def test_category_specific_b12_not_b1(self):
        p=copy.deepcopy(self.product);p['title']='Vitamin B12 1000 mcg';p['ingredients']=[]
        self.assertEqual(revita.category_for(p,self.template.categories)[0],'17703')
        p['title']='Магнезий 200 mg';self.assertEqual(revita.category_for(p,self.template.categories)[0],'17613')
        p['title']='Колаген';self.assertEqual(revita.category_for(p,self.template.categories)[0],'17622')
        p['title']='Витамин D3 2000 IU';self.assertEqual(revita.category_for(p,self.template.categories)[0],'17708')

    def test_fallback_has_real_category_and_review(self):
        p=copy.deepcopy(self.product);p['title']='Unknown botanical blend';p['ingredients']=[]
        cat,why=revita.category_for(p,self.template.categories)
        self.assertIn(cat,self.template.categories)
        self.assertIn('review',why)

    def test_dropdown_and_missing_manufacturer(self):
        row,report=revita.build_row(self.product,self.template,self.config)
        self.assertEqual(row['E'],'17711')
        self.assertEqual(row['NY'],'')
        self.assertEqual(row['R'],'')
        self.assertEqual(row['NZ'],'')
        self.assertIn('Manufacturer',report['issues'])
        self.assertEqual(row['LB'],16.61)
        self.assertEqual(row['LD'],33.21)
        self.assertEqual(row['HT'],'Gummies')
        self.assertEqual(row['LU'],'Magazin')
        self.assertEqual(row['HP'],'2026-10-31')
        for c,v in row.items():
            opts=self.template.options(c,row)
            if opts and v:self.assertIn(v,opts,(c,v))

    def test_known_manufacturer_and_brand(self):
        p=copy.deepcopy(self.product);p['brand']='Natural Factors'
        row,_=revita.build_row(p,self.template,self.config)
        self.assertEqual(row['NY'],'Natural Factors Nutritional Products Ltd')
        self.assertEqual(row['R'],'NATURAL FACTORS')
        self.assertEqual(row['S'],'Natural Factors')

    def test_requested_brands_are_blank_but_rows_are_exported(self):
        rows=[]
        for brand in ['Alive',"Nature’s Way",'Natures Way','Double Wood','Bärbel Drexel']:
            p=copy.deepcopy(self.product);p['brand']=brand
            cfg=copy.deepcopy(self.config)
            cfg['product_overrides']={'15789':{'columns':{'R':'NATURAL FACTORS','NY':'NOW Health Group, Inc'}}}
            row,report=revita.build_row(p,self.template,cfg)
            self.assertEqual(row['R'],'')
            self.assertEqual(row['NY'],'')
            self.assertEqual(row['S'],'')
            self.assertEqual(row['LB'],16.61)
            self.assertIn('intentionally blank',report['issues'])
            rows.append(row)
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'common.xlsx';self.template.write(rows,out)
            with zipfile.ZipFile(out) as z:
                root=etree.fromstring(z.read(self.template.sheet_path))
                self.assertEqual(len(root.find(revita.M+'sheetData')),4+len(rows))

    def test_invalid_dropdown_override_is_reported(self):
        cfg=copy.deepcopy(self.config)
        cfg['product_overrides']={'15789':{'columns':{'NY':'Nature’s Way'}}}
        p=copy.deepcopy(self.product);p['brand']='Natural Factors'
        row,report=revita.build_row(p,self.template,cfg)
        self.assertEqual(row['NY'],'')
        self.assertIn('Dropdown mismatch NY',report['issues'])

    def test_no_discount_and_stock_unknown(self):
        doc=revita.parse_doc(self.product_source)
        for e in doc.xpath('//*['+revita.CLASS('regular-price')+']'):e.getparent().remove(e)
        p=revita.parse_product(html.tostring(doc),self.url)
        self.assertEqual(p['list_price'],16.61)
        p['stock']='unknown'
        row,report=revita.build_row(p,self.template,self.config)
        self.assertEqual(row['LA'],0)
        self.assertIn('Availability unknown',report['issues'])

    def test_unknown_currency_and_redirect_rejected(self):
        src=self.product_source.decode().replace('data-currency_sign="€"','data-currency_sign="$"')
        with self.assertRaises(ValueError):revita.parse_product(src,self.url)
        with self.assertRaises(ValueError):revita.parse_product(self.product_source,self.url+'-wrong')

    def test_recommendations_do_not_change_primary(self):
        doc=revita.parse_doc(self.product_source)
        article=html.fromstring('<article class="product-miniature"><h1>Wrong</h1><span class="product-price" content="9999">9999 €</span><img src="https://revita.bg/wrong.jpg"></article>')
        doc.xpath('//section[@id="main"]')[0].append(article)
        p=revita.parse_product(html.tostring(doc),self.url)
        self.assertEqual(p['price'],16.61)
        self.assertNotIn('https://revita.bg/wrong.jpg',p['images'])

    def test_template_preservation_and_typed_prices(self):
        row,_=revita.build_row(self.product,self.template,self.config)
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'out.xlsx';self.template.write([row,row],out)
            with zipfile.ZipFile(out) as z:
                self.assertIsNone(z.testzip())
                for name,value in self.template.parts.items():
                    if name!=self.template.sheet_path:self.assertEqual(z.read(name),value,name)
                root=etree.fromstring(z.read(self.template.sheet_path))
                data=root.find(revita.M+'sheetData')
                old=self.template.root.find(revita.M+'sheetData')
                for a,b in zip(list(data)[:4],list(old)[:4]):
                    self.assertEqual(etree.tostring(a),etree.tostring(b))
                rows=data.findall(revita.M+'row')
                self.assertEqual(len(rows),6)
                values={c.get('r'):c for c in rows[4]}
                self.assertIsNone(values['LB5'].get('t'))
                self.assertEqual(values['LB5'].find(revita.M+'v').text,'16.61')
                self.assertEqual(values['L5'].get('t'),'inlineStr')
                self.assertEqual(len(root.find(revita.M+'dataValidations')),188)

    def test_template_clears_stale_rows(self):
        row,_=revita.build_row(self.product,self.template,self.config)
        with tempfile.TemporaryDirectory() as temp:
            a=Path(temp)/'a.xlsx';b=Path(temp)/'b.xlsx'
            self.template.write([row,row,row],a)
            t=revita.Template(a);t.write([row],b)
            with zipfile.ZipFile(b) as z:
                root=etree.fromstring(z.read(t.sheet_path))
                self.assertEqual(len(root.find(revita.M+'sheetData')),5)

    def test_variant_refresh_distinct_ids_prices_and_stock(self):
        p=copy.deepcopy(self.product)
        p['variant_options']={'group[1]':[('1','Small'),('2','Large')]}
        class FakeClient:
            def get(self,url):
                large='group%5B1%5D=2' in url
                price='20.00' if large else '10.00'
                return json.dumps({'id_product_attribute':2 if large else 1,
                    'product_prices':f'<div><span class="product-price" content="{price}" data-currency_sign="€">{price} €</span></div>',
                    'product_add_to_cart':'<div><button data-button-action="add-to-cart" '+('disabled="disabled"' if large else '')+'></button></div>'}).encode()
        variants=revita.expand_variants(p,self.product_source,FakeClient())
        self.assertEqual([x['variant_id'] for x in variants],['1','2'])
        self.assertEqual([x['price'] for x in variants],[10.0,20.0])
        self.assertEqual([x['stock'] for x in variants],['in','out'])

    def test_bad_variant_response_not_silently_exported(self):
        p=copy.deepcopy(self.product);p['variant_options']={'group[1]':[('1','Small')]}
        class FakeClient:
            def get(self,url):return b'{}'
        with self.assertRaises(ValueError):revita.expand_variants(p,self.product_source,FakeClient())

    def test_client_blocks_other_domains_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            client=revita.Client(cache=temp)
            with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('Network called')):
                with self.assertRaises(ValueError):client.get('https://example.com/')

    def test_full_pipeline_seven_categories_two_pages_dedup_no_network(self):
        url1=self.url;url2=self.url+'-second'
        src1=self.product_source
        src2=src1.decode().replace(url1,url2).replace('value="586"','value="587"')
        class FakeClient:
            def __init__(self,*args,**kwargs):pass
            def get(self,url):
                if url==url1:return src1
                if url==url2:return src2.encode()
                base=url.split('?')[0];second='page=2' in url
                link=url2 if second else url1
                navigation=f'<a href="{base}?page=1">1</a>' if second else f'<span rel="next" data-href="{base}?page=2"></span>'
                return f'<html><div id="js-product-list"><article class="product-miniature"><p class="product-title"><a href="{link}">Product</a></p></article></div><nav class="pagination">{navigation}</nav></html>'.encode()
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(revita,'Client',FakeClient),patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('Network called')):
                status=revita.main(['--template',str(ROOT/'templates/temu_template.xlsx'),'--config',str(ROOT/'config.json'),'--output',temp])
            summary=json.loads((Path(temp)/'summary.json').read_text())
            coverage=json.loads((Path(temp)/'coverage.json').read_text())
            self.assertEqual(status,0)
            self.assertEqual(len(coverage),7)
            self.assertTrue(all(x['pages']==2 for x in coverage))
            self.assertEqual(summary['discovered_unique_products'],2)
            self.assertEqual(summary['exported_rows'],2)
            self.assertEqual(summary['errors'],0)
            self.assertTrue(summary['complete_crawl'])


if __name__=='__main__':
    unittest.main()
