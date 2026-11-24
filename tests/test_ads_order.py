import json
import tempfile
import unittest
from itertools import permutations, product
from unittest.mock import patch

from bs4 import BeautifulSoup

from app import create_app
from app.config import Config
from app.models import AdSlot, SiteSetting, User, db


class AdsOrderTest(unittest.TestCase):
    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        with patch.multiple(Config, SQLALCHEMY_DATABASE_URI='sqlite://',
                            MEDIA_ROOT=media.name, AUTO_SYNC_INTERVAL=0):
            self.app = create_app()
        self.app.config['TESTING'] = True
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(db.session.remove)
        self.client = self.app.test_client()
        user = User(email='ads-qa@example.org', is_admin=True, is_active=True, whatsapp='+5545000000000')
        user.set_password('ads-local-test')
        db.session.add(user)
        db.session.commit()
        login = self.client.get('/admin/login')
        token = BeautifulSoup(login.data, 'html.parser').find('input', {'name': 'csrf_token'})['value']
        response = self.client.post('/admin/login', data={'email': user.email, 'password': 'ads-local-test',
                                                         'csrf_token': token})
        self.assertEqual(response.status_code, 302)
        self.keys = ['sidebar_1', 'header_top', 'sidebar_2']
        for key in self.keys:
            slot = AdSlot.query.filter_by(key=key).one()
            slot.html = '__ADCFG__' + json.dumps({'banners': [
                {'image': f'/static/{key}.png', 'title': key, 'link': '#'}
            ]})
        db.session.commit()

    def page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return BeautifulSoup(response.data, 'html.parser')

    def home_keys(self):
        return [node['data-ad-slot'] for node in self.page('/').select(
            '.pa-home-sidebar > [data-ad-slot]')]

    def assert_admin_order(self, expected):
        page = self.page('/admin/ads')
        for container in page.select('[data-ads-sortable]'):
            self.assertEqual([node['data-slot-key'] for node in container.select(
                '[data-slot-key]')], list(expected))
        self.assertEqual(len(page.select('[data-ads-sortable]')), 2)
        self.assertIsNotNone(page.select_one('[data-save-ads-order]'))

    def test_every_order_and_activation_combination(self):
        for order in permutations(self.keys):
            response = self.client.post('/admin/ads/order', json={'order': order})
            self.assertEqual(response.status_code, 200)
            for flags in product([False, True], repeat=3):
                with self.subTest(order=order, active=flags):
                    for key, active in zip(order, flags):
                        AdSlot.query.filter_by(key=key).one().is_active = active
                    db.session.commit()
                    self.assertEqual(self.home_keys(), [
                        key for key, active in zip(order, flags) if active])
                    self.assert_admin_order(order)
                    saved = SiteSetting.query.filter_by(key='ad_sidebar_order').one()
                    self.assertEqual(json.loads(saved.value), list(order))

    def test_manage_deactivate_and_reactivate_preserves_position(self):
        for key in self.keys[:2]:
            slot = AdSlot.query.filter_by(key=key).one()
            form = {'name': slot.name, 'banner_existing_image[]': f'/static/{key}.png',
                    'banner_title[]': key, 'banner_link[]': '#', 'interval_seconds': '5'}
            response = self.client.post(f'/admin/ads/{slot.id}/manage', data=form)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.home_keys(), [k for k in self.keys if k != key])
            self.assert_admin_order(self.keys)
            form['is_active'] = 'on'
            response = self.client.post(f'/admin/ads/{slot.id}/manage', data=form)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.home_keys(), self.keys)

    def test_empty_banner_does_not_reserve_space(self):
        for html in ['', '   ', '__ADCFG__{"banners": []}']:
            AdSlot.query.filter_by(key=self.keys[0]).one().html = html
            db.session.commit()
            self.assertEqual(self.home_keys(), self.keys[1:])
            self.assert_admin_order(self.keys)


if __name__ == '__main__':
    unittest.main()
