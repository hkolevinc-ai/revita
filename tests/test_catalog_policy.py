import copy, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import strict_export as export
import revita_core as core
from test_revita import product, Images, ROOT


class CatalogPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = core.Template(ROOT/'templates/temu_template.xlsx')
        cls.config = json.loads((ROOT/'config.json').read_text())

    def test_all_three_brands_export_medical_title_and_label(self):
        reviewer = Images({'https://revita.bg/label.jpg': {'text': 'Treats diabetes. Supports immunity. Made in USA'}})
        for brand in ['Alive', 'Nature’s Way', 'Double Wood']:
            p = product(brand=brand, title='Vitamin B12 treats diabetes 60 capsules')
            row, audit, images = export.screen_product(p, self.template, self.config, reviewer)
            self.assertIsNotNone(row, audit)
            self.assertEqual(row['N'], p['reference'])
            self.assertIn('treats diabetes', row['L'])
            self.assertEqual(images[0]['status'], 'pass')
            self.assertFalse(audit['medical_claim_filter'])

    def test_unknown_origin_retained_blank_and_reported(self):
        p = product(source_text="Manufacturer: Nature's Way, USA")
        r = Images({'https://revita.bg/label.jpg': {'text': 'Vitamin B12 60 capsules'}})
        row, audit, _ = export.screen_product(p, self.template, self.config, r)
        self.assertIsNotNone(row)
        self.assertEqual(row['LW'], '')
        self.assertIn('country_of_origin_not_confirmed', audit['pending_fields'])
        self.assertFalse(audit['required_data_complete'])

    def test_non_gmo_alone_is_not_an_environmental_claim(self):
        r = Images({'https://revita.bg/label.jpg': {'text': 'Non GMO Vitamin B12 supports immunity Made in USA'}})
        row, audit, images = export.screen_product(product(), self.template, self.config, r)
        self.assertEqual(images[0]['status'], 'pass')
        self.assertIn('https://revita.bg/label.jpg', row.values())

    def test_eco_image_omitted_without_dropping_product(self):
        r = Images({'https://revita.bg/label.jpg': {'text': 'Eco friendly recyclable packaging'}})
        row, audit, images = export.screen_product(product(), self.template, self.config, r)
        self.assertIsNotNone(row)
        self.assertNotIn('https://revita.bg/label.jpg', row.values())
        self.assertEqual(images[0]['status'], 'eco')
        self.assertIn('missing_image_after_eco_screening', audit['pending_fields'])

    def test_natures_way_specific_origin_evidence_supported(self):
        import datetime
        p = product(source_text='Manufacturer address only')
        with tempfile.TemporaryDirectory() as directory:
            evidence = {p['reference']: dict(country='Canada', product_id=p['id'], expected_count=p['count'],
                        original_title=p['title'], url='https://naturesway.com/products/specific-product',
                        verified_at=datetime.datetime.now(datetime.timezone.utc).date().isoformat(), evidence='Made in Canada')}
            path = Path(directory)/'origin.json'
            path.write_text(json.dumps(evidence))
            config = copy.deepcopy(self.config)
            config['origin_evidence_file'] = str(path)
            reviewer = Images({'https://revita.bg/label.jpg': {'text': 'Vitamin B12 60 capsules'}})
            row, audit, _ = export.screen_product(p, self.template, config, reviewer)
            self.assertEqual(row['LW'], 'Canada', audit)
            evidence[p['reference']]['expected_count'] = 120
            path.write_text(json.dumps(evidence))
            row, audit, _ = export.screen_product(p, self.template, config, reviewer)
            self.assertEqual(row['LW'], '')
            self.assertIn('country_of_origin_not_confirmed', audit['pending_fields'])

    def test_missing_composition_and_unreadable_label_are_reported(self):
        r = Images({'https://revita.bg/label.jpg': {'quality_ok': False, 'text': '', 'error': 'OCR unavailable'}})
        row, audit, _ = export.screen_product(product(ingredients=[], directions=[]), self.template, self.config, r)
        self.assertIsNotNone(row)
        self.assertIn('missing_ingredients', audit['pending_fields'])
        self.assertIn('missing_directions', audit['pending_fields'])
        self.assertIn('missing_image_after_eco_screening', audit['pending_fields'])

    def test_inactive_or_missing_original_sku_still_excluded(self):
        for change in [dict(active=False), dict(reference='')]:
            row, audit, _ = export.screen_product(product(**change), self.template, self.config, Images())
            self.assertIsNone(row)
            self.assertEqual(audit['decision'], 'excluded')

    def test_full_export_writes_common_and_complete_files_and_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = copy.deepcopy(self.config)
            config['template'] = str(ROOT/'templates/temu_template.xlsx')
            (base/'config.json').write_text(json.dumps(config))
            products = [product(id=str(i), reference='00'+str(i)+' SKU', brand=brand)
                        for i, brand in enumerate(['Alive', 'Nature’s Way', 'Double Wood'])]
            products[1]['source_text'] = 'No origin stated'
            (base/'catalog.json').write_text(json.dumps(products))
            unknown = Images({'https://revita.bg/label.jpg': {'text': 'Vitamin B12 capsules'}})
            with patch.object(export, 'ImageReviewer', return_value=unknown):
                result = export.main(['--config', str(base/'config.json'), '--from-catalog', str(base/'catalog.json'), '--output', str(base/'out')])
            self.assertEqual(result, 0)
            summary = json.loads((base/'out/summary.json').read_text())
            self.assertEqual(summary['exported_rows'], 3)
            self.assertEqual(summary['required_data_complete_rows'], 2)
            self.assertEqual(summary['rows_needing_data'], 1)
            self.assertEqual(summary['exported_by_brand'], {'Alive': 1, 'Nature’s Way': 1, 'Double Wood': 1})
            all_rows = core.Template(base/'out/TEMU_REVITA_ALL_001.xlsx').rows_dict('Template')[4:]
            self.assertEqual([r['N'] for r in all_rows], [p['reference'] for p in products])
            self.assertTrue((base/'out/TEMU_REVITA_COMPLETE_DATA_001.xlsx').exists())
            self.assertTrue((base/'out/needs_data.csv').exists())
