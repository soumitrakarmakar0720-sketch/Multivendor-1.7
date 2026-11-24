"""Initialize a NEW database before starting Gunicorn. Never use against legacy production."""
import os
from app import app, db, User
from werkzeug.security import generate_password_hash
if not os.environ.get('DATABASE_URL'):
    raise RuntimeError('DATABASE_URL required')
if not os.environ.get('PLATFORM_ADMIN_PASSWORD_HASH'):
    raise RuntimeError('PLATFORM_ADMIN_PASSWORD_HASH required')
with app.app_context():
    db.create_all()
    username=os.environ.get('ADMIN_USERNAME','').strip()
    password=os.environ.get('ADMIN_PASSWORD','')
    if not username or len(password)<12:
        raise RuntimeError('Set ADMIN_USERNAME and ADMIN_PASSWORD (12+ characters)')
    if not User.query.filter_by(username=username).first():
        db.session.add(User(username=username,password_hash=generate_password_hash(password),role='admin'))
        db.session.commit()
os.execvp('gunicorn',['gunicorn','app:app','--bind','0.0.0.0:'+os.environ.get('PORT','10000'),'--workers','2','--threads','4'])
