import os, unittest
from test_app import app, db
from werkzeug.security import generate_password_hash
class SaaSTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING']=True
        with app.app_context(): db.drop_all(); db.create_all()
        self.c=app.test_client()
        os.environ['PLATFORM_ADMIN_PASSWORD_HASH']=generate_password_hash('platform-test-password')
    def post(self,path,**data):
        with self.c.session_transaction() as s: s['_csrf']='test'
        return self.c.post(path,data=dict(data,_csrf='test'))
    def test_isolation(self):
        for name in ('alpha','bravo'):
            self.assertEqual(self.post('/merchant/register',slug=name,name=name,email=name+'@example.com',password='merchant-test-password').status_code,200)
        self.assertEqual(self.post('/merchant/login',email='alpha@example.com',password='merchant-test-password').status_code,403)
        self.assertEqual(self.post('/merchant/platform/1/status',status='approved',reviewed='yes').status_code,403)
        self.post('/merchant/platform',password='platform-test-password')
        for i in (1,2): self.assertEqual(self.post(f'/merchant/platform/{i}/status',status='approved',reviewed='yes').status_code,302)
        self.post('/merchant/logout')
        self.post('/merchant/login',email='alpha@example.com',password='merchant-test-password')
        self.assertEqual(self.post('/merchant/bills',customer='PrivateAlpha',phone='9876543210',description='Item',total='123').status_code,302)
        self.assertIn(b'PrivateAlpha',self.c.get('/merchant/dashboard').data)
        self.assertEqual(self.c.get('/dashboard').status_code,403)
        self.post('/merchant/logout')
        self.post('/merchant/login',email='bravo@example.com',password='merchant-test-password')
        self.assertNotIn(b'PrivateAlpha',self.c.get('/merchant/dashboard').data)
        self.assertEqual(self.c.get('/merchant/platform').status_code,200) # login screen only
