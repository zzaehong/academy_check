import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from academy.server import backup, connect, make_server, set_password, load_credentials


class AppTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'test.sqlite3')
        self.server = make_server(self.path, port=0)
        set_password(self.path, 'test-password-123')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ''
        self.csrf = ''
        status, data = self.request('POST','login',{'password':'test-password-123'})
        self.assertEqual(status,200)
        self.csrf = data['csrf']

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, data=None):
        conn = http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        conn.request(method,'/api/'+path,body=json.dumps(data) if data is not None else None,headers={'Cookie':self.cookie,'X-CSRF-Token':self.csrf,'Content-Type':'application/json'})
        response = conn.getresponse()
        cookie = response.getheader('Set-Cookie')
        if cookie:
            self.cookie = cookie.split(';')[0]
        result = json.loads(response.read())
        status = response.status
        conn.close()
        return status,result

    def create(self, kind, data):
        status,result=self.request('POST',kind,data)
        self.assertEqual(status,200,result)
        return result

    def test_env_hash_rotation_and_permissions(self):
        env = Path(self.temp.name) / '.env'
        env.write_text('OTHER_SETTING=keep\nADMIN_PASSWORD=old-plaintext\n')
        password = 'new-test-password-456'
        set_password(self.path, password, env)
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(password, env.read_text())
        self.assertNotIn('ADMIN_PASSWORD=', env.read_text())
        self.assertIn('OTHER_SETTING=keep', env.read_text())
        self.assertEqual(self.request('GET', 'students')[0], 401)
        self.assertEqual(self.request('POST','login',{'password':'test-password-123'})[0],401)
        status, result = self.request('POST','login',{'password':password})
        self.assertEqual(status, 200)
        self.csrf = result['csrf']
        load_credentials(self.path, env)
        self.assertEqual(self.request('GET', 'students')[0], 200)
        env.chmod(0o644)
        with self.assertRaises(ValueError):
            load_credentials(self.path, env)
        env.chmod(0o600)
        env.write_text('ADMIN_PASSWORD_HASH=invalid\n')
        with self.assertRaises(ValueError):
            load_credentials(self.path, env)
        set_password(self.path, password, env)
        restored = str(Path(self.temp.name) / 'fresh.sqlite3')
        from academy.server import initialize
        initialize(restored)
        load_credentials(restored, env)
        with connect(restored) as db, connect(self.path) as original:
            self.assertEqual(tuple(db.execute('SELECT salt,password FROM admin').fetchone()),
                             tuple(original.execute('SELECT salt,password FROM admin').fetchone()))

    def test_split_scores_validation_edit_and_backup(self):
        student = self.create('students', {'name':'영역별 시험'})
        base = dict(student_id=student['id'], category_id=1, date='2026-09-13',
                    title='중간고사', score_mode='split', objective_max=70,
                    written_max=30, objective_score=60.5, written_score=25)
        score = self.create('scores', dict(base, score=1))
        self.assertEqual(score['score'], 85.5)
        self.assertEqual(score['objective_max'], 70)
        for changes in ({'objective_score':71}, {'written_score':31},
                        {'objective_score':-1}, {'written_score':None},
                        {'objective_max':80}, {'written_max':float('nan')},
                        {'score_mode':'other'}, {'objective_max':True}):
            self.assertEqual(self.request('POST','scores',dict(base, **changes))[0],400)
        missing = dict(base)
        del missing['written_max']
        self.assertEqual(self.request('POST','scores',missing)[0],400)
        zero = self.create('scores',dict(base,objective_score=0,written_score=0))
        self.assertEqual(zero['score'],0)
        updated = dict(base,objective_max=80,written_max=20,objective_score=75.1,
                       written_score=19.2,version=score['version'])
        status, result = self.request('PUT',f"scores/{score['id']}",updated)
        self.assertEqual(status,200)
        self.assertEqual(result['score'],94.3)
        self.assertEqual(self.request('PUT',f"scores/{score['id']}",updated)[0],409)
        saved = self.request('GET','scores')[1][0]
        self.assertEqual(saved['written_max'],20)
        target = backup(self.path,Path(self.temp.name)/'split-backup')
        with connect(target) as db:
            row = db.execute('SELECT * FROM scores WHERE id=?',(score['id'],)).fetchone()
            self.assertEqual(row['objective_score'],75.1)
            self.assertEqual(row['score_mode'],'split')
        total = {k:v for k,v in base.items() if k not in
                 ('score_mode','objective_max','written_max','objective_score','written_score')}
        status, result = self.request('PUT',f"scores/{score['id']}",dict(total,score=88,score_mode='total',version=2))
        self.assertEqual(status,200)
        self.assertIsNone(result['objective_score'])
        self.assertIsNone(result['written_max'])
        self.assertEqual(result['score'],88)
        status, result = self.request('PUT',f"scores/{score['id']}",dict(base,version=3))
        self.assertEqual(status,200)
        self.assertEqual(result['score_mode'],'split')

    def test_existing_score_database_migration(self):
        from academy.server import SCHEMA, initialize
        legacy = str(Path(self.temp.name)/'legacy.sqlite3')
        with connect(legacy) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO students(name) VALUES('기존 원생')")
            db.execute("INSERT INTO categories(name) VALUES('기존 카테고리')")
            db.execute("INSERT INTO scores(student_id,category_id,date,title,score,difficulty) VALUES(1,1,'2026-09-13','기존 시험',91.5,'')")
        initialize(legacy)
        initialize(legacy)
        with connect(legacy) as db:
            record = db.execute('SELECT * FROM scores').fetchone()
            self.assertEqual(record['score'],91.5)
            self.assertEqual(record['version'],1)
            self.assertEqual(record['score_mode'],'total')
            self.assertIsNone(record['objective_max'])
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_auth_csrf_logout(self):
        cookie=self.cookie
        self.cookie=''
        for kind in ('students','books','notes','scores','categories'):
            self.assertEqual(self.request('GET',kind)[0],401)
        self.cookie=cookie
        csrf=self.csrf
        self.csrf='wrong'
        self.assertEqual(self.request('POST','students',{'name':'학생'})[0],403)
        self.csrf=csrf
        self.assertEqual(self.request('POST','logout',{})[0],200)
        self.assertEqual(self.request('GET','students')[0],401)

    def test_records_validation_conflicts_and_cascade(self):
        a=self.create('students',{'name':'김학생'})
        b=self.create('students',{'name':'김학생','school':'다른 학교'})
        self.assertNotEqual(a['id'],b['id'])
        self.assertEqual(self.request('POST','students',{'name':'   '})[0],400)
        categories=self.request('GET','categories')[1]
        self.assertEqual(len(categories),5)
        self.assertEqual(self.request('POST','categories',{'name':' '+categories[0]['name']+' '})[0],400)
        book=self.create('books',{'student_id':a['id'],'title':'English 1','status':'사용 중'})
        note=self.create('notes',{'student_id':a['id'],'date':'2025-01-01','content':'상담'})
        note2=self.create('notes',{'student_id':a['id'],'date':'2025-01-01','content':'추가 상담'})
        self.assertEqual(self.request('POST','notes',{'student_id':a['id'],'content':'날짜 없음'})[0],400)
        base={'student_id':a['id'],'category_id':categories[0]['id'],'date':'2025-01-01','title':'시험','difficulty':''}
        scores=[self.create('scores',dict(base,score=score)) for score in (0,92.5,100)]
        for invalid in (-1,101,'',None):
            self.assertEqual(self.request('POST','scores',dict(base,score=invalid))[0],400)
        self.assertEqual(self.request('POST','scores',dict(base,score=50,max_score=200))[0],400)
        changes={k:v for k,v in a.items() if k!='id'}
        changes['school']='수정 학교'
        self.assertEqual(self.request('PUT',f"students/{a['id']}",changes)[0],200)
        self.assertEqual(self.request('PUT',f"students/{a['id']}",changes)[0],409)
        book_data={k:v for k,v in book.items() if k!='id'}
        book_data['status']='사용 완료'
        status,updated=self.request('PUT',f"books/{book['id']}",book_data)
        self.assertEqual(status,200)
        updated.pop('id')
        updated['status']='사용 중'
        self.assertEqual(self.request('PUT',f"books/{book['id']}",updated)[0],200)
        self.assertEqual(len(self.request('GET','notes')[1]),2)
        self.assertEqual(self.request('DELETE',f"notes/{note['id']}",{'version':1})[0],200)
        self.assertEqual(self.request('PUT',f"notes/{note['id']}",dict(student_id=a['id'],date='2025-01-01',content='부활',version=1))[0],404)
        self.assertEqual(self.request('DELETE',f"scores/{scores[0]['id']}",{'version':1})[0],200)
        self.assertEqual(len(self.request('GET','scores')[1]),2)
        self.assertEqual(len(self.request('GET','notes')[1]),1)
        self.assertEqual(self.request('DELETE',f"students/{a['id']}",{'version':1})[0],409)
        self.assertEqual(self.request('DELETE',f"students/{a['id']}",{'version':2})[0],200)
        for kind in ('books','notes','scores'):
            self.assertEqual(self.request('GET',kind)[1],[])
        self.assertEqual(self.request('GET','students')[1],[b])
        self.assertEqual(len(self.request('GET','categories')[1]),5)

    def test_fifty_students_backup_and_restart(self):
        for i in range(50):
            self.create('students',{'name':f'학생{i}'})
        self.create('books',{'student_id':1,'title':'교재','status':'사용 중'})
        self.create('notes',{'student_id':1,'date':'2026-09-13','content':'상담'})
        self.create('scores',{'student_id':1,'category_id':1,'date':'2026-09-13','title':'시험','score':83.5})
        target=backup(self.path,Path(self.temp.name)/'backups')
        with connect(target) as restored:
            self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(restored.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(restored.execute('SELECT COUNT(*) FROM students').fetchone()[0],50)
            for kind in ('books','notes','scores'):
                self.assertEqual(restored.execute(f'SELECT student_id FROM {kind}').fetchone()[0],1)
            self.assertEqual(restored.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)
        second=make_server(str(target),port=0)
        try:
            with connect(second.db_path) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM students').fetchone()[0],50)
        finally:
            second.server_close()


if __name__ == '__main__':
    unittest.main()
