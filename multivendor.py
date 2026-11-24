"""Isolated merchant workspace; never uses legacy payment credentials or tables."""
import os, secrets, hmac, re, requests
from datetime import datetime, timezone
from flask import jsonify
from decimal import Decimal, InvalidOperation
from functools import wraps
from flask import Blueprint, request, session, abort, redirect, url_for, render_template, flash
from werkzeug.security import generate_password_hash, check_password_hash

def install(app, db):
    bp = Blueprint('merchant', __name__, url_prefix='/merchant')
    class Merchant(db.Model):
        __tablename__ = 'saas_merchant'
        id = db.Column(db.Integer, primary_key=True)
        slug = db.Column(db.String(64), unique=True, nullable=False)
        name = db.Column(db.String(120), nullable=False)
        email = db.Column(db.String(160), unique=True, nullable=False)
        password_hash = db.Column(db.String(256), nullable=False)
        address = db.Column(db.String(300), default='')
        registration_ref = db.Column(db.String(100), default='')
        status = db.Column(db.String(20), nullable=False, default='pending')
    class MerchantConfig(db.Model):
        __tablename__ = 'saas_merchant_config'
        merchant_id = db.Column(db.Integer, db.ForeignKey('saas_merchant.id'), primary_key=True)
        billing_type = db.Column(db.String(20), nullable=False, default='retail')
        gateway = db.Column(db.String(20), nullable=False, default='none')
        cycle = db.Column(db.String(20), nullable=False, default='monthly')
        price = db.Column(db.Numeric(12,2), nullable=False, default=0)
        paid_until = db.Column(db.Date, nullable=True)
        upi_enabled = db.Column(db.Boolean, nullable=False, default=False)
        upi_id = db.Column(db.String(120), default='')
    class Bill(db.Model):
        __tablename__ = 'saas_bill'
        id = db.Column(db.Integer, primary_key=True)
        merchant_id = db.Column(db.Integer, db.ForeignKey('saas_merchant.id'), nullable=False, index=True)
        token = db.Column(db.String(64), unique=True, nullable=False)
        customer = db.Column(db.String(120), nullable=False)
        description = db.Column(db.String(300), nullable=False)
        total = db.Column(db.Numeric(12,2), nullable=False)
        brand = db.Column(db.String(120), nullable=False)
        address = db.Column(db.String(300), default='')
    class SaleDetails(db.Model):
        __tablename__ = 'saas_sale_details'
        bill_id = db.Column(db.Integer, db.ForeignKey('saas_bill.id'), primary_key=True)
        phone = db.Column(db.String(30), default='')
        email = db.Column(db.String(160), default='')
        customer_address = db.Column(db.String(300), default='')
        method = db.Column(db.String(20), nullable=False, default='unpaid')
        paid = db.Column(db.Boolean, nullable=False, default=False)
    class Archive(db.Model):
        __tablename__ = 'saas_bill_archive'
        bill_id = db.Column(db.Integer, db.ForeignKey('saas_bill.id'), primary_key=True)
    class GatewayOrder(db.Model):
        __tablename__ = 'saas_gateway_order'
        bill_id = db.Column(db.Integer, db.ForeignKey('saas_bill.id'), primary_key=True)
        order_id = db.Column(db.String(64), unique=True, nullable=False)
        paid_at = db.Column(db.DateTime(timezone=True))
    class Settlement(db.Model):
        __tablename__ = 'saas_settlement'
        id = db.Column(db.Integer, primary_key=True)
        merchant_id = db.Column(db.Integer, db.ForeignKey('saas_merchant.id'), nullable=False)
        amount = db.Column(db.Numeric(12,2), nullable=False)
        reference = db.Column(db.String(120), unique=True, nullable=False)
        created = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    class Staff(db.Model):
        __tablename__ = 'saas_staff'
        id = db.Column(db.Integer, primary_key=True)
        merchant_id = db.Column(db.Integer, db.ForeignKey('saas_merchant.id'), nullable=False)
        email = db.Column(db.String(160), unique=True, nullable=False)
        password_hash = db.Column(db.String(256), nullable=False)
        role = db.Column(db.String(20), nullable=False)
        active = db.Column(db.Boolean, default=True, nullable=False)
    def gateway_enabled(c):
        return bool(c and c.gateway=='platform' and os.getenv('CASHFREE_CLIENT_ID') and os.getenv('CASHFREE_CLIENT_SECRET') and os.getenv('SAAS_PLATFORM_COLLECTIONS')=='yes')
    def cf(method, path, payload=None):
        base='https://api.cashfree.com/pg' if os.getenv('CASHFREE_ENV')=='production' else 'https://sandbox.cashfree.com/pg'
        r=requests.request(method,base+path,json=payload,headers={'x-client-id':os.environ['CASHFREE_CLIENT_ID'],'x-client-secret':os.environ['CASHFREE_CLIENT_SECRET'],'x-api-version':'2023-08-01'},timeout=15)
        r.raise_for_status(); return r.json()
    @bp.post('/invoice/<token>/gateway')
    def gateway_start(token):
        b=Bill.query.filter_by(token=token).with_for_update().first_or_404(); d=db.session.get(SaleDetails,b.id); m=db.session.get(Merchant,b.merchant_id)
        if not d or d.paid or d.method!='gateway' or not m or m.status!='approved' or not gateway_enabled(db.session.get(MerchantConfig,b.merchant_id)) or db.session.get(Archive,b.id): abort(409)
        g=db.session.get(GatewayOrder,b.id)
        if not g:
            g=GatewayOrder(bill_id=b.id,order_id='saas_'+secrets.token_hex(16)); db.session.add(g); db.session.commit()
        try:
            try: result=cf('GET','/orders/'+g.order_id)
            except requests.HTTPError as e:
                if e.response.status_code!=404: raise
                result=cf('POST','/orders',{'order_id':g.order_id,'order_amount':float(b.total),'order_currency':'INR','customer_details':{'customer_id':'saas_'+str(b.id),'customer_phone':d.phone},'order_meta':{'return_url':url_for('merchant.gateway_return',token=b.token,_external=True),'notify_url':url_for('merchant.gateway_webhook',_external=True)}})
            if result.get('order_status')=='PAID': return redirect(url_for('merchant.gateway_return',token=token))
            return render_template('saas_gateway.html',payment_session=result['payment_session_id'],mode='production' if os.getenv('CASHFREE_ENV')=='production' else 'sandbox')
        except (requests.RequestException,KeyError,ValueError):
            flash('Gateway unavailable. Retry the same invoice.','error'); return redirect(url_for('merchant.invoice',token=token))
    def verify_gateway(g):
        result=cf('GET','/orders/'+g.order_id)
        b=Bill.query.filter_by(id=g.bill_id).with_for_update().first_or_404(); d=db.session.get(SaleDetails,b.id)
        if result.get('order_status')=='PAID' and result.get('order_id')==g.order_id and result.get('order_currency')=='INR' and Decimal(str(result.get('order_amount')))==b.total and d and d.method=='gateway':
            d.paid=True
            if not g.paid_at: g.paid_at=datetime.now(timezone.utc)
            db.session.commit(); return True
        return False
    @bp.get('/invoice/<token>/gateway-return')
    def gateway_return(token):
        b=Bill.query.filter_by(token=token).first_or_404(); g=db.session.get(GatewayOrder,b.id)
        try:
            if g: verify_gateway(g)
        except (requests.RequestException,ValueError,InvalidOperation): flash('Payment verification pending.','error')
        return redirect(url_for('merchant.invoice',token=token))
    @bp.post('/gateway/webhook')
    def gateway_webhook():
        import base64, hashlib
        ts=request.headers.get('x-webhook-timestamp',''); sig=request.headers.get('x-webhook-signature',''); secret=os.getenv('CASHFREE_CLIENT_SECRET','')
        if not secret or not ts or not hmac.compare_digest(sig,base64.b64encode(hmac.new(secret.encode(),ts.encode()+request.get_data(),hashlib.sha256).digest()).decode()): abort(403)
        data=request.get_json(silent=True) or {}; oid=data.get('data',{}).get('order',{}).get('order_id')
        g=GatewayOrder.query.filter_by(order_id=oid).first() if oid else None
        try:
            if g: verify_gateway(g)
        except (requests.RequestException,ValueError,InvalidOperation): return jsonify(ok=False),503
        return jsonify(ok=True)
    @bp.post('/invoice/<int:bid>/archive')
    def archive_bill(bid):
        m=owner(); b=Bill.query.filter_by(id=bid,merchant_id=m.id).with_for_update().first_or_404(); d=db.session.get(SaleDetails,b.id)
        if not d or not d.paid: abort(400,'Only settled bills may be archived in this release.')
        a=db.session.get(Archive,b.id)
        if a: db.session.delete(a)
        else: db.session.add(Archive(bill_id=b.id))
        db.session.commit(); return redirect(url_for('merchant.workspace',section='invoices'))
    def payout_rows():
        rows=[]
        for m in Merchant.query.all():
            collected=db.session.query(db.func.coalesce(db.func.sum(Bill.total),0)).join(GatewayOrder,GatewayOrder.bill_id==Bill.id).filter(Bill.merchant_id==m.id,GatewayOrder.paid_at.isnot(None)).scalar()
            settled=db.session.query(db.func.coalesce(db.func.sum(Settlement.amount),0)).filter_by(merchant_id=m.id).scalar()
            rows.append((m,collected,settled,collected-settled))
        return rows
    @bp.post('/platform/settlement/<int:mid>')
    def record_settlement(mid):
        platform(); Merchant.query.filter_by(id=mid).with_for_update().first_or_404(); amount=money('amount'); ref=field('reference',120)
        balance=next(r[3] for r in payout_rows() if r[0].id==mid)
        if not ref or amount>balance or Settlement.query.filter_by(reference=ref).first(): abort(400,'Invalid settlement')
        db.session.add(Settlement(merchant_id=mid,amount=amount,reference=ref)); db.session.commit(); return redirect(url_for('merchant.section_page',section='payouts'))
    @bp.route('/staff',methods=['GET','POST'])
    def staff_manage():
        m=owner()
        if session.get('saas_staff_id'): abort(403)
        if request.method=='POST':
            email=field('email',160).lower(); password=request.form.get('password',''); role=field('role',20)
            if not email or not 12<=len(password)<=128 or role not in ('cashier','waiter','kitchen') or Staff.query.filter_by(email=email).first() or Merchant.query.filter_by(email=email).first(): abort(400,'Invalid or duplicate staff details')
            db.session.add(Staff(merchant_id=m.id,email=email,password_hash=generate_password_hash(password),role=role)); db.session.commit()
        return render_template('saas_staff.html',staff=Staff.query.filter_by(merchant_id=m.id).all())
    @bp.post('/staff/<int:sid>/toggle')
    def staff_toggle(sid):
        m=owner()
        if session.get('saas_staff_id'): abort(403)
        st=Staff.query.filter_by(id=sid,merchant_id=m.id).first_or_404(); st.active=not st.active; db.session.commit(); return redirect(url_for('merchant.staff_manage'))
    @bp.before_request
    def staff_guard():
        if not session.get('saas_staff_id'): return
        st=db.session.get(Staff,session['saas_staff_id'])
        if not st or not st.active or st.merchant_id!=session.get('merchant_id'): session.clear(); abort(403)
        ep=request.endpoint; section=(request.view_args or {}).get('section')
        if ep in ('merchant.logout','merchant.invoice','merchant.invoice_qr'): return
        allowed={'cashier':('merchant.create_bill','merchant.ticket_invoice','merchant.settle_cash','merchant.confirm_upi','merchant.gateway_start','merchant.gateway_return'),'waiter':(), 'kitchen':('merchant.ticket_ready',)}
        if ep in allowed[st.role]: return
        if ep=='merchant.workspace' and section in {'cashier':('billing','invoices','orders'),'waiter':('orders','tables'),'kitchen':('kitchen',)}[st.role] and (request.method=='GET' or section=='orders' and st.role=='waiter'): return
        abort(403)
    class CustomerAccount(db.Model):
        __tablename__ = 'saas_customer_account'
        id = db.Column(db.Integer, primary_key=True)
        phone = db.Column(db.String(10), unique=True, nullable=False)
        name = db.Column(db.String(120), nullable=False)
        password_hash = db.Column(db.String(256), nullable=False)
        verified = db.Column(db.Boolean, nullable=False, default=False)
    class CustomerInvite(db.Model):
        __tablename__ = 'saas_customer_invite'
        phone = db.Column(db.String(10), primary_key=True)
        digest = db.Column(db.String(64), nullable=False)
        expires = db.Column(db.BigInteger, nullable=False)
    def customer_page(section, **kw):
        return render_template('customer_hub.html', section=section, **kw)
    @bp.route('/customer/verify-admin', methods=['GET','POST'])
    def customer_verify_admin():
        platform()
        if request.method == 'POST':
            import hashlib, time
            phone = required_phone()
            if request.form.get('reviewed') != 'yes': abort(400, 'Verify control of the mobile number first.')
            if CustomerAccount.query.filter_by(phone=phone).first(): abort(409)
            code = secrets.token_urlsafe(24)
            invite = db.session.get(CustomerInvite, phone) or CustomerInvite(phone=phone)
            invite.digest = hashlib.sha256(code.encode()).hexdigest(); invite.expires = int(time.time())+900
            db.session.add(invite); db.session.commit()
            return customer_page('verify-admin', code=code)
        return customer_page('verify-admin')
    @bp.route('/customer/register', methods=['GET','POST'])
    def customer_register():
        if request.method == 'POST':
            import hashlib, time
            from sqlalchemy.exc import IntegrityError
            phone = required_phone(); name = field('name',120); password = request.form.get('password','')
            invite = CustomerInvite.query.filter_by(phone=phone).with_for_update().first()
            code = request.form.get('code','')
            if not name or not 12<=len(password)<=128 or not invite or invite.expires<int(time.time()) or not hmac.compare_digest(invite.digest,hashlib.sha256(code.encode()).hexdigest()):
                flash('Check your details and verification code.','error'); return redirect(url_for('merchant.customer_register'))
            db.session.add(CustomerAccount(phone=phone,name=name,password_hash=generate_password_hash(password),verified=True)); db.session.delete(invite)
            try: db.session.commit()
            except IntegrityError:
                db.session.rollback(); flash('Account already exists.','error'); return redirect(url_for('merchant.customer_login'))
            return redirect(url_for('merchant.customer_login'))
        return customer_page('register')
    @bp.route('/customer/login', methods=['GET','POST'])
    def customer_login():
        if request.method == 'POST':
            phone = required_phone(); a = CustomerAccount.query.filter_by(phone=phone,verified=True).first()
            password=request.form.get('password','')
            if a and len(password)<=128 and check_password_hash(a.password_hash,password):
                session.clear(); session['hub_customer_id']=a.id; return redirect(url_for('merchant.customer_hub'))
            flash('Invalid mobile number or password.','error')
        return customer_page('login')
    @bp.get('/customer')
    @bp.get('/customer/<section>')
    def customer_hub(section='home'):
        a=db.session.get(CustomerAccount,session.get('hub_customer_id')) if session.get('hub_customer_id') else None
        if not a or not a.verified: return redirect(url_for('merchant.customer_login'))
        if section not in ('home','bills','payments','wallet','refunds','profile'): abort(404)
        rows=db.session.query(Bill,SaleDetails,Merchant).join(SaleDetails,SaleDetails.bill_id==Bill.id).join(Merchant,Merchant.id==Bill.merchant_id).filter(SaleDetails.phone==a.phone).order_by(Merchant.name,Bill.id.desc()).all()
        groups={}
        for b,d,m in rows: groups.setdefault((m.id,m.name),[]).append((b,d,m))
        return customer_page(section,a=a,rows=rows,groups=groups,paid=sum((b.total for b,d,m in rows if d.paid),Decimal(0)),due=sum((b.total for b,d,m in rows if not d.paid),Decimal(0)))
    @bp.post('/customer/logout')
    def customer_logout():
        session.clear(); return redirect(url_for('merchant.customer_login'))
    @bp.after_request
    def customer_private(response):
        if request.path.startswith('/merchant/customer'):
            response.headers['Cache-Control']='no-store'
            response.headers['Referrer-Policy']='no-referrer'
        return response
    class Catalog(db.Model):
        __tablename__ = 'saas_catalog'
        id = db.Column(db.Integer, primary_key=True)
        merchant_id = db.Column(db.Integer, db.ForeignKey('saas_merchant.id'), nullable=False, index=True)
        name = db.Column(db.String(120), nullable=False)
        price = db.Column(db.Numeric(12,2), nullable=False)
        stock = db.Column(db.Integer, nullable=False, default=0)
    class MenuCategory(db.Model):
        __tablename__ = 'saas_menu_category'
        catalog_id = db.Column(db.Integer, db.ForeignKey('saas_catalog.id', ondelete='CASCADE'), primary_key=True)
        name = db.Column(db.String(80), nullable=False, default='Other')
    class DiningTable(db.Model):
        __tablename__ = 'saas_dining_table'
        id = db.Column(db.Integer, primary_key=True)
        merchant_id = db.Column(db.Integer, db.ForeignKey('saas_merchant.id'), nullable=False, index=True)
        name = db.Column(db.String(80), nullable=False)
        token = db.Column(db.String(64), unique=True, nullable=False)
        __table_args__ = (db.UniqueConstraint('merchant_id','name'),)
    class Ticket(db.Model):
        __tablename__ = 'saas_ticket'
        id = db.Column(db.Integer, primary_key=True)
        merchant_id = db.Column(db.Integer, db.ForeignKey('saas_merchant.id'), nullable=False, index=True)
        table_id = db.Column(db.Integer, db.ForeignKey('saas_dining_table.id'))
        description = db.Column(db.String(300), nullable=False)
        total = db.Column(db.Numeric(12,2), nullable=False)
        state = db.Column(db.String(20), default='open', nullable=False)
        kitchen = db.Column(db.String(20), default='new', nullable=False)
        bill_id = db.Column(db.Integer, db.ForeignKey('saas_bill.id'))
    @bp.errorhandler(400)
    def invalid_form(error):
        db.session.rollback()
        if request.path.startswith("/merchant/customer"):
            flash(error.description,"error"); return redirect(url_for("merchant.customer_verify_admin" if session.get("platform_admin") else "merchant.customer_register" if request.endpoint=="merchant.customer_register" else "merchant.customer_login"))
        flash(error.description if error.description != 'The browser (or proxy) sent a request that this server could not understand.' else 'Invalid form values. Check the required fields and try again.', 'error')
        return redirect(url_for('merchant.workspace',section=(request.view_args or {}).get('section','billing')) if session.get('merchant_id') else url_for('merchant.admin'))
    def required_phone():
        phone = request.form.get('phone', '').strip()
        if not re.fullmatch(r'[0-9]{10}', phone):
            abort(400, 'Enter a valid 10-digit customer mobile number.')
        return phone
    def money(key):
        try:
            v=Decimal(field(key,30))
            if v.is_finite() and 0<v<=9999999999 and v==v.quantize(Decimal('.01')): return v
        except (InvalidOperation, ValueError): pass
        abort(400,'Invalid amount')
    def restaurant(m):
        c=db.session.get(MerchantConfig,m.id)
        if not c or c.billing_type!='restaurant': abort(403)
    @bp.route('/workspace/<section>', methods=['GET','POST'])
    def workspace(section):
        m=owner(); c=db.session.get(MerchantConfig,m.id)
        if section not in ('billing','invoices','branding','payments','catalog','tables','kitchen','orders','reports','trash'): abort(404)
        if section in ('tables','kitchen','orders'): restaurant(m)
        if request.method=='POST':
            if section=='catalog':
                name=field('name',120)
                try: stock=int(field('stock',10) or '0')
                except ValueError: abort(400)
                if not name or not 0<=stock<=100000000: abort(400,'Enter an item name and nonnegative whole-number stock.')
                item=Catalog(merchant_id=m.id,name=name,price=money('price'),stock=stock)
                db.session.add(item); db.session.flush(); db.session.add(MenuCategory(catalog_id=item.id,name=field('category',80) or 'Other'))
            elif section=='catalog-edit':
                abort(405)
            elif section=='tables':
                name=field('name',80)
                if not name or DiningTable.query.filter_by(merchant_id=m.id,name=name).first(): abort(400)
                db.session.add(DiningTable(merchant_id=m.id,name=name,token=secrets.token_urlsafe(32)))
            elif section=='orders':
                raw=field('table_id',20); table=None
                if raw:
                    if not raw.isdigit(): abort(400,'Invalid table')
                    table=DiningTable.query.filter_by(merchant_id=m.id,id=int(raw)).with_for_update().first_or_404()
                    for old in Ticket.query.filter_by(merchant_id=m.id,table_id=table.id).all():
                        if old.bill_id:
                            d=db.session.get(SaleDetails,old.bill_id)
                            if not d or not d.paid: abort(400,'Settle the existing bill first.')
                ids=request.form.getlist('product_id'); qtys=request.form.getlist('quantity')
                if len(ids)!=len(qtys) or len(ids)>100: abort(400,'Invalid cart')
                lines=[]; total=Decimal(0); seen=set()
                for raw_id,raw_qty in zip(ids,qtys):
                    try: pid=int(raw_id); qty=int(raw_qty)
                    except ValueError: abort(400,'Invalid quantity')
                    if qty==0: continue
                    if pid in seen or not 1<=qty<=1000: abort(400,'Invalid cart quantity')
                    seen.add(pid)
                    item=Catalog.query.filter_by(merchant_id=m.id,id=pid).first_or_404()
                    lines.append(f'{item.name} × {qty}'); total+=item.price*qty
                desc='; '.join(lines)
                if not desc or len(desc)>300 or total>Decimal('9999999999.99'): abort(400,'Select items or split this KOT into smaller orders.')
                db.session.add(Ticket(merchant_id=m.id,table_id=table.id if table else None,description=desc,total=total,state='open',kitchen='new'))
            else: abort(405)
            db.session.commit(); flash('Saved successfully.','success'); return redirect(url_for('merchant.workspace',section=section, **({'table': table.id if table else 'parcel'} if section=='orders' else {})))
        bills=Bill.query.filter_by(merchant_id=m.id).order_by(Bill.id.desc()).all()
        bills=[b for b in bills if bool(db.session.get(Archive,b.id))==(section=='trash')] if section in ('invoices','trash') else bills
        categories={x.catalog_id:x.name for x in MenuCategory.query.join(Catalog, Catalog.id==MenuCategory.catalog_id).filter(Catalog.merchant_id==m.id).all()}
        occupied={}
        for t in Ticket.query.filter_by(merchant_id=m.id).all():
            if t.table_id:
                d=db.session.get(SaleDetails,t.bill_id) if t.bill_id else None
                if not t.bill_id or not d or not d.paid: occupied[t.table_id]=t.bill_id or 0
        return page('dashboard',categories=categories,occupied=occupied,section=section,m=m,config=c,bills=bills,total=sum(b.total for b in bills),catalog=Catalog.query.filter_by(merchant_id=m.id).all(),tables=DiningTable.query.filter_by(merchant_id=m.id).all(),tickets=Ticket.query.filter_by(merchant_id=m.id).order_by(Ticket.id.desc()).all())
    @bp.post('/catalog/<int:cid>/edit')
    def edit_catalog(cid):
        m=owner(); item=Catalog.query.filter_by(id=cid,merchant_id=m.id).with_for_update().first_or_404()
        name=field('name',120)
        try: stock=int(field('stock',10))
        except ValueError: abort(400,'Invalid stock')
        if not name or not 0<=stock<=100000000: abort(400,'Invalid item details')
        item.name=name; item.price=money('price'); item.stock=stock
        category=db.session.get(MenuCategory,item.id) or MenuCategory(catalog_id=item.id)
        category.name=field('category',80) or 'Other'; db.session.add(category)
        db.session.commit(); return redirect(url_for('merchant.workspace',section='catalog'))
    @bp.post('/catalog/<int:cid>/delete')
    def delete_catalog(cid):
        m=owner(); item=Catalog.query.filter_by(id=cid,merchant_id=m.id).with_for_update().first_or_404()
        category=db.session.get(MenuCategory,item.id)
        if category: db.session.delete(category); db.session.flush()
        db.session.delete(item); db.session.commit(); return redirect(url_for('merchant.workspace',section='catalog'))
    @bp.post('/ticket/<int:tid>/ready')
    def ticket_ready(tid):
        m=owner(); restaurant(m); t=Ticket.query.filter_by(id=tid,merchant_id=m.id).with_for_update().first_or_404()
        t.kitchen='ready'; db.session.commit(); return redirect(url_for('merchant.workspace',section='kitchen'))
    @bp.post('/ticket/<int:tid>/invoice')
    def ticket_invoice(tid):
        m=owner(); restaurant(m); t=Ticket.query.filter_by(id=tid,merchant_id=m.id).first_or_404()
        if t.table_id:
            DiningTable.query.filter_by(id=t.table_id,merchant_id=m.id).with_for_update().first_or_404()
        db.session.refresh(t)
        if t.bill_id:
            b=Bill.query.filter_by(id=t.bill_id,merchant_id=m.id).first_or_404(); return redirect(url_for('merchant.invoice',token=b.token))
        tickets=Ticket.query.filter_by(merchant_id=m.id,table_id=t.table_id,bill_id=None).order_by(Ticket.id).with_for_update().all() if t.table_id else Ticket.query.filter_by(id=t.id,merchant_id=m.id,bill_id=None).with_for_update().all()
        if not tickets: abort(409)
        phone=required_phone(); method=field('method',20) or 'unpaid'; c=db.session.get(MerchantConfig,m.id)
        if method not in ('cash','upi','unpaid','gateway'): abort(400,'Invalid payment method')
        if method=='gateway' and not gateway_enabled(c): abort(400,'Platform gateway unavailable')
        if method=='cash' and field('confirmed',10)!='yes': abort(400,'Confirm cash received')
        if method=='upi' and (not c or not c.upi_enabled or not c.upi_id): abort(400,'Enable UPI first')
        total=sum(x.total for x in tickets); desc='; '.join(x.description for x in tickets)
        if total>Decimal('9999999999.99'): abort(400,'Bill amount too large')
        if len(desc)>300: desc='Dining order — KOT '+', '.join(str(x.id) for x in tickets); desc=desc[:300]
        b=Bill(merchant_id=m.id,token=secrets.token_urlsafe(32),customer=field('customer',120) or ('Dine-in' if t.table_id else 'Parcel'),description=desc,total=total,brand=m.name,address=m.address)
        db.session.add(b); db.session.flush(); db.session.add(SaleDetails(bill_id=b.id,phone=phone,email=field('email',160),customer_address=field('customer_address',300),method=method,paid=method=='cash'))
        for x in tickets: x.bill_id=b.id; x.state='invoiced'
        db.session.commit(); return redirect(url_for('merchant.invoice',token=b.token))
    @bp.post('/invoice/<int:bid>/settle-cash')
    def settle_cash(bid):
        m=owner(); b=Bill.query.filter_by(id=bid,merchant_id=m.id).with_for_update().first_or_404(); d=db.session.get(SaleDetails,b.id)
        if not d or d.paid or d.method=='gateway' or field('confirmed',10)!='yes': abort(400,'Confirm cash received on an unpaid invoice')
        d.method='cash'; d.paid=True; db.session.commit(); return redirect(url_for('merchant.invoice',token=b.token))
    @bp.get('/table/<int:tid>/qr')
    def table_qr(tid):
        import io, qrcode
        from flask import send_file
        m=owner(); restaurant(m); t=DiningTable.query.filter_by(id=tid,merchant_id=m.id).first_or_404()
        buf=io.BytesIO(); qrcode.make(url_for('merchant.table_view',token=t.token,_external=True)).save(buf,format='PNG'); buf.seek(0)
        return send_file(buf,mimetype='image/png',as_attachment=True,download_name=f'table-{t.id}.png')
    @bp.get('/table/view/<token>')
    def table_view(token):
        t=DiningTable.query.filter_by(token=token).first_or_404(); m=db.session.get(Merchant,t.merchant_id)
        if not m or m.status!='approved': abort(404)
        return page('table_public',m=m,table=t)
    def page(kind, **kw): return render_template('merchant.html', kind=kind, gateway_enabled=gateway_enabled, **kw)
    def field(key, n): return request.form.get(key,'').strip()[:n]
    def owner():
        m = db.session.get(Merchant, session.get('merchant_id')) if session.get('merchant_id') else None
        if not m or m.status != 'approved': abort(403)
        return m
    def platform():
        if not session.get('platform_admin') or not os.environ.get('PLATFORM_ADMIN_PASSWORD_HASH'): abort(403)
        if not hmac.compare_digest(session['platform_admin'], __import__('hashlib').sha256(os.environ['PLATFORM_ADMIN_PASSWORD_HASH'].encode()).hexdigest()): abort(403)
    @bp.route('/register', methods=['GET','POST'])
    def register():
        if request.method == 'POST':
            slug, email, name = field('slug',64).lower(), field('email',160).lower(), field('name',120)
            password = request.form.get('password','')
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,63}',slug) or slug=='owlstore' or not name or '@' not in email or not 12<=len(password)<=128: abort(400)
            if Merchant.query.filter((Merchant.slug==slug)|(Merchant.email==email)).first(): abort(409)
            db.session.add(Merchant(slug=slug,email=email,name=name,password_hash=generate_password_hash(password),address=field('address',300),registration_ref=field('registration_ref',100)))
            db.session.commit(); return page('pending')
        return page('register')
    @bp.route('/login', methods=['GET','POST'])
    def login():
        if request.method=='POST':
            m=Merchant.query.filter_by(email=field('email',160).lower()).first()
            if m and m.status=='approved' and check_password_hash(m.password_hash,request.form.get('password','')):
                session.clear(); session['merchant_id']=m.id; return redirect(url_for('merchant.dashboard'))
            st=Staff.query.filter_by(email=field('email',160).lower(),active=True).first()
            if st and check_password_hash(st.password_hash,request.form.get('password','')) and db.session.get(Merchant,st.merchant_id).status=='approved':
                session.clear(); session['merchant_id']=st.merchant_id; session['saas_staff_id']=st.id; return redirect(url_for('merchant.workspace',section={'cashier':'billing','waiter':'orders','kitchen':'kitchen'}[st.role]))
            abort(403)
        return page('login')
    @bp.route('/platform', methods=['GET','POST'])
    def admin():
        if request.method=='POST':
            p=os.environ.get('PLATFORM_ADMIN_PASSWORD_HASH','')
            if not p or not check_password_hash(p,request.form.get('password','')): abort(403)
            session.clear(); session['platform_admin']=__import__('hashlib').sha256(p.encode()).hexdigest()
            return redirect(url_for('merchant.admin'))
        if not session.get('platform_admin'): return page('platform_login')
        platform()
        return platform_page('home')
    def platform_page(section, selected=None):
        platform()
        merchants=Merchant.query.order_by(Merchant.id).all()
        invoice_count, invoice_total = db.session.query(db.func.count(Bill.id), db.func.coalesce(db.func.sum(Bill.total),0)).one()
        invoice_rows = db.session.query(Bill, Merchant.name).join(Merchant, Merchant.id==Bill.merchant_id).order_by(Bill.id.desc()).limit(100).all()
        return render_template('platform_admin.html', section=section, merchants=merchants, selected=selected, configs={c.merchant_id:c for c in MerchantConfig.query.all()}, invoice_count=invoice_count, invoice_total=invoice_total, invoice_rows=invoice_rows,payout_rows=payout_rows() if section=='payouts' else [])
    @bp.get('/platform/<section>')
    def section_page(section):
        platform()
        if section not in ('merchants','kyc','subscriptions','gateways','customers','reports','transactions','settings','payouts','refunds'): abort(404)
        return platform_page(section)
    @bp.get('/platform/merchants/<int:mid>')
    def merchant_profile(mid):
        platform()
        return platform_page('profile', Merchant.query.get_or_404(mid))
    @bp.route('/platform/add', methods=['GET','POST'])
    def add_merchant():
        if request.method=='GET': return platform_page('add')
        platform()
        slug, email, name = field('slug',64).lower(), field('email',160).lower(), field('name',120)
        password = request.form.get('password','')
        errors = []
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,63}',slug) or slug=='owlstore': errors.append('Use a unique merchant handle of 3–64 letters, numbers or hyphens, starting with a letter or number. Example: royal-restaurant.')
        if not name: errors.append('Business name is required.')
        if not re.fullmatch(r'[^\s@]+@[^\s@]+',email): errors.append('Enter a valid email address.')
        if not 12<=len(password)<=128: errors.append('Password must contain 12–128 characters.')
        kind=field('billing_type',20)
        if kind not in ('retail','restaurant'): errors.append('Select Retail or Restaurant.')
        if errors:
            for error in errors: flash(error,'error')
            return redirect(url_for('merchant.add_merchant'))
        if Merchant.query.filter((Merchant.slug==slug)|(Merchant.email==email)).first():
            flash('Merchant email or handle already exists.','error'); return redirect(url_for('merchant.add_merchant'))
        m=Merchant(slug=slug,email=email,name=name,password_hash=generate_password_hash(password),address=field('address',300),registration_ref=field('registration_ref',100),status='pending')
        db.session.add(m); db.session.flush()
        db.session.add(MerchantConfig(merchant_id=m.id,billing_type=kind))
        db.session.commit(); flash('Merchant created. Complete verification and approve below.')
        return redirect(url_for('merchant.section_page',section='merchants'))
    @bp.post('/platform/<int:mid>/status')
    def status(mid):
        platform(); m=Merchant.query.get_or_404(mid); state=field('status',20)
        if state not in ('approved','rejected','suspended'): abort(400)
        if state=='approved' and request.form.get('reviewed')!='yes':
            flash('Confirm offline business verification before approval.','error'); return redirect(url_for('merchant.merchant_profile',mid=mid))
        m.status=state; db.session.commit(); flash('Merchant status updated.','success'); return redirect(url_for('merchant.merchant_profile',mid=mid))
    @bp.post('/platform/<int:mid>/config')
    def config(mid):
        platform(); Merchant.query.get_or_404(mid)
        from datetime import date
        kind, gateway, cycle = field('billing_type',20), field('gateway',20), field('cycle',20)
        if kind not in ('retail','restaurant') or gateway not in ('none','platform','custom') or cycle not in ('monthly','yearly'): abort(400)
        try:
            price=Decimal(field('price',30))
            if not price.is_finite() or not 0<=price<=9999999999 or price!=price.quantize(Decimal('.01')): abort(400)
            until=date.fromisoformat(field('paid_until',10)) if field('paid_until',10) else None
        except (ValueError,InvalidOperation): abort(400)
        c=db.session.get(MerchantConfig,mid) or MerchantConfig(merchant_id=mid)
        c.billing_type=kind; c.gateway=gateway; c.cycle=cycle; c.price=price; c.paid_until=until
        db.session.add(c); db.session.commit(); flash('Configuration saved.','success'); return redirect(url_for('merchant.merchant_profile',mid=mid))
    @bp.post('/upi-settings')
    def upi_settings():
        m=owner(); c=db.session.get(MerchantConfig,m.id) or MerchantConfig(merchant_id=m.id)
        vpa=field('upi_id',120); enabled=request.form.get('enabled')=='yes'
        if enabled and not re.fullmatch(r'[A-Za-z0-9._-]+@[A-Za-z0-9.-]+',vpa): abort(400)
        c.upi_id=vpa; c.upi_enabled=enabled; db.session.add(c); db.session.commit(); return redirect(url_for('merchant.dashboard'))
    @bp.get('/dashboard')
    def dashboard():
        m=owner(); bills=Bill.query.filter_by(merchant_id=m.id).order_by(Bill.id.desc()).all(); return page('dashboard',section='home',m=m,bills=bills,total=sum(b.total for b in bills), config=db.session.get(MerchantConfig,m.id))
    @bp.post('/settings')
    def settings():
        m=owner(); name=field('name',120)
        if not name: abort(400)
        m.name=name; m.address=field('address',300); db.session.commit(); return redirect(url_for('merchant.dashboard'))
    @bp.post('/bills')
    def create_bill():
        m=owner()
        c=db.session.get(MerchantConfig,m.id)
        method=field('method',20) or 'unpaid'
        if method not in ('unpaid','cash','upi','gateway'): abort(400,'Invalid payment method')
        if method=='gateway' and not gateway_enabled(c): abort(400,'Platform gateway unavailable')
        if method=='cash' and request.form.get('confirmed')!='yes': abort(400,'Confirm cash received before recording a cash sale.')
        if method=='upi' and (not c or not c.upi_enabled): abort(400,'Enable UPI in payment settings first.')
        phone = required_phone()
        customer=field('customer',120) or 'Walk-in customer'
        ids=request.form.getlist('product_id'); quantities=request.form.getlist('quantity')
        if len(ids)!=len(quantities) or len(ids)>50: abort(400,'Invalid cart')
        cart={}
        for raw,q in zip(ids,quantities):
            if not raw: continue
            try: pid=int(raw); qty=int(q)
            except ValueError: abort(400,'Invalid quantity')
            if not 1<=qty<=10000: abort(400,'Invalid quantity')
            cart[pid]=cart.get(pid,0)+qty
        if cart:
            amount=Decimal(0); lines=[]
            for pid,qty in sorted(cart.items()):
                item=Catalog.query.filter_by(id=pid,merchant_id=m.id).with_for_update().first_or_404()
                if c and c.billing_type=='retail':
                    if item.stock<qty: abort(400,'Insufficient stock for '+item.name)
                    item.stock-=qty
                amount+=item.price*qty; lines.append(f'{item.name} × {qty}')
            desc='; '.join(lines)
            if len(desc)>300 or amount>Decimal('9999999999.99'): abort(400,'Cart too large; split the invoice.')
        else:
            amount=money('total'); desc=field('description',300)
            if not desc: abort(400,'Enter a description or select products.')
        b=Bill(merchant_id=m.id,token=secrets.token_urlsafe(32),customer=customer,description=desc,total=amount,brand=m.name,address=m.address)
        db.session.add(b); db.session.flush()
        db.session.add(SaleDetails(bill_id=b.id,phone=phone,email=field('email',160),customer_address=field('customer_address',300),method=method,paid=method=='cash'))
        db.session.commit(); return redirect(url_for('merchant.invoice',token=b.token))
    @bp.get('/invoice/<token>/upi.png')
    def invoice_qr(token):
        import io, qrcode
        from urllib.parse import urlencode
        from flask import send_file
        b=Bill.query.filter_by(token=token).first_or_404(); m=db.session.get(Merchant,b.merchant_id); c=db.session.get(MerchantConfig,b.merchant_id); d=db.session.get(SaleDetails,b.id)
        if not m or m.status!='approved' or not c or not c.upi_enabled or not c.upi_id or not d or d.paid or d.method!='upi': abort(404)
        uri='upi://pay?'+urlencode(dict(pa=c.upi_id,pn=m.name,am=str(b.total),cu='INR',tn=f'Invoice {b.id}'))
        buf=io.BytesIO(); qrcode.make(uri).save(buf,format='PNG'); buf.seek(0); return send_file(buf,mimetype='image/png',max_age=0)
    @bp.post('/invoice/<int:bid>/confirm-upi')
    def confirm_upi(bid):
        m=owner(); b=Bill.query.filter_by(id=bid,merchant_id=m.id).with_for_update().first_or_404(); d=db.session.get(SaleDetails,b.id)
        if not d or d.method!='upi' or request.form.get('confirmed')!='yes': abort(400,'Verify bank credit first.')
        d.paid=True; db.session.commit(); return redirect(url_for('merchant.invoice',token=b.token))
    @bp.get('/invoice/<token>')
    def invoice(token):
        b=Bill.query.filter_by(token=token).first_or_404(); m=db.session.get(Merchant,b.merchant_id)
        if not m or m.status!='approved': abort(404)
        return page('invoice',b=b,invoice_tickets=Ticket.query.filter_by(merchant_id=b.merchant_id,bill_id=b.id).all(),details=db.session.get(SaleDetails,b.id),config=db.session.get(MerchantConfig,b.merchant_id))
    @bp.post('/logout')
    def logout(): session.clear(); return redirect(url_for('merchant.login'))
    @app.before_request
    def separate_sessions():
        if (session.get('merchant_id') or session.get('platform_admin')) and request.blueprint!='merchant' and request.endpoint not in ('static','health','cashfree_webhook'): abort(403)
    @app.cli.command('upgrade-saas')
    def upgrade():
        for model in (Merchant,Bill,MerchantConfig,Catalog,MenuCategory,DiningTable,Ticket,SaleDetails,CustomerAccount,CustomerInvite,Archive,GatewayOrder,Settlement,Staff): model.__table__.create(db.engine,checkfirst=True)
        print('Merchant tables ready; legacy tables unchanged')
    app.register_blueprint(bp)
