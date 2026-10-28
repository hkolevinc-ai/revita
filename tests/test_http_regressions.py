"""Regression tests for the failures seen in revita-temu-results.zip."""
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import revita_core as revita

ROOT=Path(__file__).resolve().parents[1]


class Response:
    status=200
    def __init__(self,body,url,content_type='text/html',encoding=''):
        self.body,self.url=body,url
        self.headers={'Content-Type':content_type,'Content-Encoding':encoding}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self):return self.body


class HttpRegressions(unittest.TestCase):
    def test_canonical_mismatch_accepts_only_exact_category_product_id(self):
        body=(ROOT/'tests/fixtures/product-canonical-mismatch.html').read_bytes()
        url=revita.BASE+'/shop/osteomuuv-super-grizha-za-stavite-1431-mg'
        hint={'id':'1313','reference':'2684 NF','brand':'Natural Factors'}
        p=revita.parse_product(body,url,hint)
        self.assertEqual(p['id'],'1313')
        self.assertEqual(p['url'],url)
        self.assertEqual(p['count'],120)
        self.assertEqual(p['price'],41.83)
        with tempfile.TemporaryDirectory() as temp:
            c=revita.Client(cache=Path(temp)/'cache',diagnostics=Path(temp)/'diag')
            c.validate(body,'product',url,expected_id='1313')
            with self.assertRaises(revita.InvalidResponse):c.validate(body,'product',url,expected_id='28854')
            with self.assertRaises(revita.InvalidResponse):c.validate(body,'product',url)
        with self.assertRaises(ValueError):revita.parse_product(body,url,{**hint,'id':'28854'})

    def test_real_ajax_page_two_has_seventeen_products(self):
        source=(ROOT/'tests/fixtures/category-page2.json').read_bytes()
        hints,pages,total=revita.parse_listing(source,revita.BASE+'/alive')
        self.assertEqual(len(hints),17)
        self.assertEqual(total,41)
        self.assertIn(revita.BASE+'/alive?page=2',pages)

    def test_real_natures_way_json_includes_all_fourteen_page_urls(self):
        source=(ROOT/'tests/fixtures/category-natures-way.json').read_bytes()
        hints,pages,total=revita.parse_listing(source,revita.BASE+'/natures-way')
        self.assertEqual(len(hints),24)
        self.assertEqual(total,318)
        for page in range(2,15):
            self.assertIn(revita.BASE+'/natures-way?page='+str(page),pages)

    def test_old_parser_failure_reproduced_for_real_json(self):
        source=(ROOT/'tests/fixtures/category-page2.json').read_bytes()
        doc=revita.parse_doc(source)
        self.assertFalse(doc.xpath('//*[@id="js-product-list"]'))
        hints,_,_=revita.parse_listing(source,revita.BASE+'/alive')
        self.assertEqual(len(hints),17)

    def test_empty_http_200_retried_with_fresh_query_and_not_cached(self):
        body=(ROOT/'tests/fixtures/category-page2.json').read_bytes()
        url=revita.BASE+'/alive?page=2'
        requests=[]
        def open_response(request,**kwargs):
            requests.append(request.full_url)
            return Response(b'' if len(requests)==1 else body,request.full_url,'application/json')
        with tempfile.TemporaryDirectory() as temp:
            client=revita.Client(delay=0,cache=Path(temp)/'cache',diagnostics=Path(temp)/'diag')
            with patch.object(client.opener,'open',side_effect=open_response),patch('revita_core.time.sleep'):
                result=client.get(url,kind='listing')
            self.assertEqual(result,body)
            self.assertEqual(len(requests),2)
            self.assertTrue(all('_revita_export=' in r for r in requests))
            self.assertNotEqual(requests[0],requests[1])
            self.assertEqual(next((Path(temp)/'cache').glob('*.html')).read_bytes(),body)
            self.assertEqual(client.attempts[0]['received_bytes'],0)

    def test_empty_body_never_written_as_valid_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            client=revita.Client(delay=0,cache=Path(temp)/'cache',diagnostics=Path(temp)/'diag')
            with patch.object(client.opener,'open',return_value=Response(b'',revita.BASE+'/alive')),patch('revita_core.time.sleep'):
                with self.assertRaises(RuntimeError):client.get(revita.BASE+'/alive',kind='listing')
            self.assertEqual(len(list((Path(temp)/'cache').glob('*.html'))),0)
            self.assertEqual(len(client.attempts),5)

    def test_invalid_old_cache_is_ignored(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            client=revita.Client(delay=0,cache=Path(temp)/'cache',diagnostics=Path(temp)/'diag')
            url=revita.BASE+'/alive?page=2'
            cache=client.cache/(hashlib.sha256(url.encode()).hexdigest()+'.html')
            cache.write_bytes(b'')
            body=(ROOT/'tests/fixtures/category-page2.json').read_bytes()
            with patch.object(client.opener,'open',return_value=Response(body,url,'application/json')),patch('revita_core.time.sleep'):
                self.assertEqual(client.get(url,kind='listing'),body)
            self.assertEqual(cache.read_bytes(),body)

    def test_gzip_response_is_decoded_before_product_validation(self):
        body=(ROOT/'tests/fixtures/product.html').read_bytes()
        url=revita.first(revita.parse_doc(body),'//link[@rel="canonical"]/@href')
        with tempfile.TemporaryDirectory() as temp:
            client=revita.Client(delay=0,cache=Path(temp)/'cache',diagnostics=Path(temp)/'diag')
            response=Response(gzip.compress(body),url,encoding='gzip')
            with patch.object(client.opener,'open',return_value=response),patch('revita_core.time.sleep'):
                self.assertEqual(client.get(url,kind='product'),body)

    def test_wrong_json_current_page_is_rejected(self):
        body=(ROOT/'tests/fixtures/category-page2.json').read_bytes()
        with tempfile.TemporaryDirectory() as temp:
            client=revita.Client(cache=Path(temp)/'cache',diagnostics=Path(temp)/'diag')
            with self.assertRaises(revita.InvalidResponse):client.validate(body,'listing',revita.BASE+'/alive?page=1')
