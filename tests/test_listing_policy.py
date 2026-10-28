import copy,json,tempfile,unittest
from pathlib import Path
from strict_export import screen_product
import revita_core as core
from test_revita import product,Images

ROOT=Path(__file__).resolve().parents[1]
class ListingPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template=core.Template(ROOT/'templates/temu_template.xlsx')
        cls.config=json.loads((ROOT/'config.json').read_text())
    def test_optional_retailer_claims_cleaned_original_sku_preserved(self):
        p=product(source_text='Made in USA\nTreats arthritis and supports immunity')
        row,a,_=screen_product(p,self.template,self.config,Images())
        self.assertIsNotNone(row,a);self.assertEqual(row['N'],'001-A NF')
        self.assertNotIn('arthritis',row['T']);self.assertNotIn('supports',row['T'])
        self.assertTrue(a['removed_retailer_claims'])
    def test_medical_claim_on_actual_label_still_excluded(self):
        image=Images({'https://revita.bg/label.jpg':{'text':'Vitamin B12 treats diabetes Made in USA'}})
        row,a,_=screen_product(product(),self.template,self.config,image)
        self.assertIsNone(row);self.assertIn('medical_claim_on_packaging',a['reasons'])
    def test_general_health_label_not_automatically_approved(self):
        image=Images({'https://revita.bg/label.jpg':{'text':'Supports immune health Made in USA'}})
        row,a,_=screen_product(product(),self.template,self.config,image)
        self.assertIsNone(row);self.assertIn('no_claim_free_product_images',a['reasons'])
    def test_specific_dietary_designation_not_environmental_claim(self):
        image=Images({'https://revita.bg/label.jpg':{'text':'Vitamin B12 Non GMO Made in USA 60 capsules'}})
        row,a,_=screen_product(product(),self.template,self.config,image)
        self.assertIsNotNone(row,a)
    def test_genuine_eco_label_is_not_exported(self):
        image=Images({'https://revita.bg/label.jpg':{'text':'Vitamin B12 Eco friendly recyclable bottle Made in USA'}})
        row,a,_=screen_product(product(),self.template,self.config,image)
        self.assertIsNone(row);self.assertIn('no_claim_free_product_images',a['reasons'])
    def test_origin_still_not_guessed_from_brand_address(self):
        image=Images({'https://revita.bg/label.jpg':{'text':'Vitamin B12 dietary supplement'}})
        row,a,_=screen_product(product(source_text='Manufacturer: Nature’s Way, USA'),self.template,self.config,image)
        self.assertIsNone(row);self.assertIn('country_of_origin_not_confirmed',a['reasons'])
    def test_mandatory_fda_negative_disclaimer_not_treatment_promise(self):
        image=Images({'https://revita.bg/label.jpg':{'text':'Vitamin B12 Made in USA. This product is not intended to diagnose, treat, cure or prevent any disease.'}})
        row,a,_=screen_product(product(),self.template,self.config,image)
        self.assertIsNotNone(row,a)
    def test_magnesium_complex_not_confused_with_vitamin_b(self):
        p=product(title='Магнезиев комплекс с 8 форми на магнезий')
        cat,_=core.category_for(p,self.template.categories)
        self.assertEqual(cat,'17613')
        p=product(title='Мио-инозитол 1000 mg, 60 капсули')
        self.assertEqual(core.category_for(p,self.template.categories)[0],'17706')
