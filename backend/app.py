from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, send_from_directory, session, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from datetime import datetime, timedelta
from functools import wraps
from sqlalchemy import text
from dateutil.relativedelta import relativedelta
import bcrypt
import os
import smtplib
import uuid
import json
import hmac
import secrets
import html as html_lib
import stripe
try:
    from backend.marketing import GUIDES, SEGMENT_PAGES
except ImportError:
    from marketing import GUIDES, SEGMENT_PAGES
try:
    from backend import storage   # Railway: gunicorn backend.app:app
    from backend.transcription import (
        MAX_AUDIO_BYTES,
        TranscriptionConfigurationError,
        TranscriptionProviderError,
        transcribe_audio,
        transcription_is_configured,
    )
except ImportError:
    import storage                # local: run from inside backend/
    from transcription import (
        MAX_AUDIO_BYTES,
        TranscriptionConfigurationError,
        TranscriptionProviderError,
        transcribe_audio,
        transcription_is_configured,
    )

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PRICE_ID      = os.environ.get('STRIPE_PRICE_ID', '')
STRIPE_ANNUAL_PRICE_ID = os.environ.get('STRIPE_ANNUAL_PRICE_ID', '')
STRIPE_WEBHOOK_SECRET= os.environ.get('STRIPE_WEBHOOK_SECRET', '')
# Optional: if Connect events (account.updated, connected checkout.session.completed)
# are delivered to a SEPARATE webhook endpoint, its signing secret goes here.
STRIPE_CONNECT_WEBHOOK_SECRET = os.environ.get('STRIPE_CONNECT_WEBHOOK_SECRET', '')
TRIAL_DAYS           = 14
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

_BASE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_BASE)
storage.init(_ROOT)

app = Flask(__name__,
    template_folder=os.path.join(_ROOT, 'frontend', 'templates'),
    static_folder=os.path.join(_ROOT, 'frontend', 'static'))

app.secret_key = os.environ.get('SECRET_KEY')
if not app.secret_key:
    if os.environ.get('RAILWAY_ENVIRONMENT'):
        raise RuntimeError('SECRET_KEY must be configured in production.')
    app.secret_key = secrets.token_hex(32)

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://dinx@localhost/fieldbase_saas')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'index'
login_manager.login_message = ''

# ─────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────

class Company(db.Model):
    __tablename__ = 'companies'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(200), nullable=False)
    slug       = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active              = db.Column(db.Boolean, default=True)
    stripe_customer_id     = db.Column(db.String(100))
    stripe_subscription_id = db.Column(db.String(100))
    subscription_status    = db.Column(db.String(20))   # trialing, active, past_due, canceled
    trial_ends_at          = db.Column(db.DateTime)
    stripe_connect_id       = db.Column(db.String(100))  # connected Express account (collects job payments)
    connect_charges_enabled = db.Column(db.Boolean, default=False)  # true once Stripe onboarding is complete
    trade_type              = db.Column(db.String(100))
    acquisition_source      = db.Column(db.String(100))
    acquisition_medium      = db.Column(db.String(100))
    acquisition_campaign    = db.Column(db.String(100))
    acquisition_content     = db.Column(db.String(100))
    acquisition_landing     = db.Column(db.String(300))
    invoice_reminders_enabled = db.Column(db.Boolean, default=False)
    client_notifications_enabled = db.Column(db.Boolean, default=False)
    users                  = db.relationship('User', backref='company', lazy=True)
    jobs                   = db.relationship('Job', backref='company', lazy=True)


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id            = db.Column(db.Integer, primary_key=True)
    company_id    = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    email         = db.Column(db.String(200), unique=True, nullable=False)
    password_hash = db.Column(db.LargeBinary, nullable=False)
    name          = db.Column(db.String(200), nullable=False)
    role          = db.Column(db.String(20), nullable=False, default='employee')  # owner or employee
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    is_active     = db.Column(db.Boolean, default=True)
    hourly_rate   = db.Column(db.Float)


class Job(db.Model):
    __tablename__ = 'jobs'
    id             = db.Column(db.Integer, primary_key=True)
    company_id     = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    title          = db.Column(db.String(200), nullable=False)
    platform       = db.Column(db.String(50), nullable=False, default='manual')
    location       = db.Column(db.String(300))
    start_time     = db.Column(db.DateTime, nullable=False)
    end_time       = db.Column(db.DateTime, nullable=False)
    status         = db.Column(db.String(50), default='scheduled')
    tech_assigned  = db.Column(db.String(100))
    tech_pay       = db.Column(db.Float)
    job_pay        = db.Column(db.Float)
    tech_confirmed  = db.Column(db.Boolean, default=False)
    confirmed_at    = db.Column(db.DateTime)
    clock_in_at     = db.Column(db.DateTime)
    clock_out_at    = db.Column(db.DateTime)
    completed_at    = db.Column(db.DateTime)
    employee_notes  = db.Column(db.Text)
    invoice_sent         = db.Column(db.Boolean, default=False)
    invoice_sent_at      = db.Column(db.DateTime)
    payment_received     = db.Column(db.Boolean, default=False)
    amount_paid          = db.Column(db.Float)
    stripe_payment_link  = db.Column(db.String(500))
    notes             = db.Column(db.Text)
    client_name       = db.Column(db.String(200))
    client_company    = db.Column(db.String(200))
    client_email      = db.Column(db.String(200))
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    external_job_id   = db.Column(db.String(200))
    clock_in_lat      = db.Column(db.Float)
    clock_in_lng      = db.Column(db.Float)
    job_lat           = db.Column(db.Float)
    job_lng           = db.Column(db.Float)
    client_id         = db.Column(db.Integer, db.ForeignKey('clients.id'))
    job_template_id   = db.Column(db.Integer, db.ForeignKey('job_templates.id'))
    closeout_checklist = db.Column(db.Text, default='[]')
    signature_name    = db.Column(db.String(200))
    signature_filename = db.Column(db.String(300))
    signed_at         = db.Column(db.DateTime)
    signature_required = db.Column(db.Boolean, default=False)


class JobPhoto(db.Model):
    __tablename__ = 'job_photos'
    id          = db.Column(db.Integer, primary_key=True)
    job_id      = db.Column(db.Integer, db.ForeignKey('jobs.id'), nullable=False)
    company_id  = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    filename    = db.Column(db.String(300), nullable=False)
    uploaded_by = db.Column(db.String(200))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


class JobDocument(db.Model):
    __tablename__ = 'job_documents'
    id            = db.Column(db.Integer, primary_key=True)
    job_id        = db.Column(db.Integer, db.ForeignKey('jobs.id'), nullable=False)
    company_id    = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    filename      = db.Column(db.String(300), nullable=False)
    original_name = db.Column(db.String(300))
    uploaded_by   = db.Column(db.String(200))
    uploaded_at   = db.Column(db.DateTime, default=datetime.utcnow)


class Client(db.Model):
    __tablename__ = 'clients'
    id            = db.Column(db.Integer, primary_key=True)
    company_id    = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False, index=True)
    name          = db.Column(db.String(200), nullable=False)
    company_name  = db.Column(db.String(200))
    email         = db.Column(db.String(200))
    phone         = db.Column(db.String(50))
    address       = db.Column(db.String(300))
    billing_terms = db.Column(db.String(100), default='Net 30')
    access_notes  = db.Column(db.Text)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('company_id', 'email', name='uq_client_company_email'),
    )


class JobTemplate(db.Model):
    __tablename__ = 'job_templates'
    id               = db.Column(db.Integer, primary_key=True)
    company_id       = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False, index=True)
    name             = db.Column(db.String(200), nullable=False)
    title            = db.Column(db.String(200), nullable=False)
    platform         = db.Column(db.String(50), default='manual')
    duration_minutes = db.Column(db.Integer, default=120)
    default_tech_pay = db.Column(db.Float)
    default_job_pay  = db.Column(db.Float)
    notes            = db.Column(db.Text)
    checklist        = db.Column(db.Text, default='[]')
    require_signature = db.Column(db.Boolean, default=False)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class InvoiceRecord(db.Model):
    __tablename__ = 'invoices'
    id             = db.Column(db.Integer, primary_key=True)
    company_id     = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False, index=True)
    job_id         = db.Column(db.Integer, db.ForeignKey('jobs.id'), index=True)
    client_id      = db.Column(db.Integer, db.ForeignKey('clients.id'))
    number         = db.Column(db.String(50), nullable=False)
    status         = db.Column(db.String(30), nullable=False, default='draft', index=True)
    issue_date     = db.Column(db.DateTime, default=datetime.utcnow)
    due_date       = db.Column(db.DateTime)
    client_name    = db.Column(db.String(200))
    client_company = db.Column(db.String(200))
    client_email   = db.Column(db.String(200))
    line_items     = db.Column(db.Text, default='[]')
    subtotal       = db.Column(db.Float, default=0)
    tax_rate       = db.Column(db.Float, default=0)
    tax_amount     = db.Column(db.Float, default=0)
    total          = db.Column(db.Float, default=0)
    amount_paid    = db.Column(db.Float, default=0)
    notes          = db.Column(db.Text)
    stripe_checkout_session_id = db.Column(db.String(200))
    stripe_checkout_url = db.Column(db.String(500))
    sent_at        = db.Column(db.DateTime)
    viewed_at      = db.Column(db.DateTime)
    paid_at        = db.Column(db.DateTime)
    reminder_sent_at = db.Column(db.DateTime)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('company_id', 'number', name='uq_invoice_company_number'),
    )


class DispatchEvent(db.Model):
    __tablename__ = 'dispatch_events'
    id          = db.Column(db.Integer, primary_key=True)
    company_id  = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False, index=True)
    job_id      = db.Column(db.Integer, db.ForeignKey('jobs.id'), nullable=False, index=True)
    event_type  = db.Column(db.String(50), nullable=False)
    recipient   = db.Column(db.String(200))
    status      = db.Column(db.String(30), default='sent')
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)


class Conflict(db.Model):
    __tablename__ = 'conflicts'
    id          = db.Column(db.Integer, primary_key=True)
    company_id  = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    job_a_id    = db.Column(db.Integer, db.ForeignKey('jobs.id'))
    job_b_id    = db.Column(db.Integer, db.ForeignKey('jobs.id'))
    detected_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved    = db.Column(db.Boolean, default=False)


class PlatformCredential(db.Model):
    __tablename__ = 'platform_credentials'
    id         = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    platform   = db.Column(db.String(50), nullable=False)   # workmarket, fieldnation
    api_key    = db.Column(db.Text)
    api_secret = db.Column(db.Text)
    enabled    = db.Column(db.Boolean, default=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('company_id', 'platform'),)


class Receipt(db.Model):
    __tablename__ = 'receipts'
    id           = db.Column(db.Integer, primary_key=True)
    company_id   = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    job_id       = db.Column(db.Integer, db.ForeignKey('jobs.id'), nullable=True)
    filename     = db.Column(db.String(300), nullable=False)
    category     = db.Column(db.String(100), default='Uncategorized')
    amount       = db.Column(db.Float)
    vendor       = db.Column(db.String(200))
    description  = db.Column(db.Text)
    uploaded_by  = db.Column(db.String(200))
    uploaded_at  = db.Column(db.DateTime, default=datetime.utcnow)


class TechStandard(db.Model):
    __tablename__ = 'tech_standards'
    id           = db.Column(db.Integer, primary_key=True)
    company_id   = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False, unique=True)
    dress_code   = db.Column(db.Text, default='Professional attire required. Company shirt, clean pants, closed-toe shoes.')
    eta_rules    = db.Column(db.Text, default='Arrive 10 minutes early. Notify client 30 minutes before arrival. Call if running late.')
    deliverables = db.Column(db.Text, default='Take before/after photos. Collect client signature. Submit job notes within 1 hour of completion.')
    safety_rules = db.Column(db.Text, default='PPE required on all job sites. Report any hazards immediately.')
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow)


class MarketingEvent(db.Model):
    __tablename__ = 'marketing_events'
    id          = db.Column(db.Integer, primary_key=True)
    company_id  = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=True)
    visitor_id  = db.Column(db.String(36), index=True)
    event_name  = db.Column(db.String(80), nullable=False, index=True)
    source      = db.Column(db.String(100))
    medium      = db.Column(db.String(100))
    campaign    = db.Column(db.String(100))
    content     = db.Column(db.String(100))
    landing     = db.Column(db.String(300))
    details     = db.Column(db.Text)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow, index=True)


def _bounded(value, limit):
    value = (value or '').strip()
    return value[:limit] or None


def _visitor_id():
    if 'marketing_visitor_id' not in session:
        session['marketing_visitor_id'] = str(uuid.uuid4())
    return session['marketing_visitor_id']


def _capture_attribution():
    """Keep first-touch acquisition data in the signed session cookie."""
    attribution = session.get('marketing_attribution')
    if attribution:
        return attribution
    attribution = {
        'source': _bounded(request.args.get('utm_source'), 100),
        'medium': _bounded(request.args.get('utm_medium'), 100),
        'campaign': _bounded(request.args.get('utm_campaign'), 100),
        'content': _bounded(request.args.get('utm_content'), 100),
        'landing': _bounded(request.path, 300),
    }
    if not any(attribution.get(key) for key in ('source', 'medium', 'campaign', 'content')):
        attribution['source'] = 'direct'
        attribution['medium'] = 'none'
    session['marketing_attribution'] = attribution
    return attribution


def _record_marketing_event(event_name, company_id=None, details=None, once=False):
    """Record a bounded, first-party funnel event without storing customer content."""
    try:
        visitor_id = _visitor_id()
        if once:
            query = MarketingEvent.query.filter_by(
                event_name=event_name,
                company_id=company_id,
            )
            if company_id is None:
                query = query.filter_by(visitor_id=visitor_id)
            if query.first():
                return
        attribution = session.get('marketing_attribution') or _capture_attribution()
        if company_id:
            company = db.session.get(Company, company_id)
            if company:
                attribution = {
                    'source': company.acquisition_source,
                    'medium': company.acquisition_medium,
                    'campaign': company.acquisition_campaign,
                    'content': company.acquisition_content,
                    'landing': company.acquisition_landing,
                }
        event = MarketingEvent(
            company_id=company_id,
            visitor_id=visitor_id,
            event_name=_bounded(event_name, 80),
            source=_bounded(attribution.get('source'), 100),
            medium=_bounded(attribution.get('medium'), 100),
            campaign=_bounded(attribution.get('campaign'), 100),
            content=_bounded(attribution.get('content'), 100),
            landing=_bounded(attribution.get('landing'), 300),
            details=json.dumps(details or {}, separators=(',', ':'))[:2000],
        )
        db.session.add(event)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        app.logger.warning('Marketing event write failed for %s: %s', event_name, exc)



import math

def _geocode_address(address):
    if not address or len(address.strip()) < 5:
        return None, None
    try:
        import requests as _req
        resp = _req.get(
            'https://nominatim.openstreetmap.org/search',
            params={'q': address, 'format': 'json', 'limit': 1},
            headers={'User-Agent': 'FieldBase/1.0'},
            timeout=5
        )
        results = resp.json()
        if results:
            return float(results[0]['lat']), float(results[0]['lon'])
    except Exception as e:
        app.logger.warning(f'Geocode failed for "{address}": {e}')
    return None, None

def _haversine_miles(lat1, lng1, lat2, lng2):
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ─────────────────────────────────────────
# DECORATORS
# ─────────────────────────────────────────

@app.after_request
def no_cache(response):
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


def _has_active_access(company):
    """True if company has an active subscription or is still in trial."""
    if company.subscription_status is None:
        return True  # account predates billing — grandfathered
    if company.subscription_status == 'active':
        return True
    if company.subscription_status == 'trialing':
        if company.trial_ends_at is None or datetime.utcnow() < company.trial_ends_at:
            return True
    return False


def owner_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'owner':
            return redirect(url_for('employee_dashboard'))
        company = Company.query.get(current_user.company_id)
        if company and not _has_active_access(company):
            return redirect(url_for('billing'))
        return f(*args, **kwargs)
    return decorated


@app.context_processor
def inject_trial_info():
    if current_user.is_authenticated and current_user.role == 'owner':
        company = Company.query.get(current_user.company_id)
        if company and company.trial_ends_at and company.subscription_status != 'active':
            days_left = max(0, (company.trial_ends_at - datetime.utcnow()).days)
            return {
                'trial_days_left': days_left,
                'founding_annual_available': bool(STRIPE_ANNUAL_PRICE_ID),
            }
    return {
        'trial_days_left': None,
        'founding_annual_available': bool(STRIPE_ANNUAL_PRICE_ID),
    }

@app.route('/sw.js')
def service_worker():
    # Served from the root so the service worker's scope covers the whole app
    # (a worker served from /static/ could only control /static/).
    return send_from_directory(os.path.join(_ROOT, 'frontend', 'static'), 'sw.js',
                               mimetype='application/javascript')


# ─────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    attribution = _capture_attribution()
    requested_plan = request.args.get('plan')
    if requested_plan in ('monthly', 'founding_annual'):
        session['requested_plan'] = requested_plan
    if request.method == 'POST':
        company_name = request.form.get('company_name', '').strip()
        name         = request.form.get('name', '').strip()
        email        = request.form.get('email', '').strip().lower()
        password     = request.form.get('password', '')
        trade_type   = request.form.get('trade_type', '').strip()

        if not all([company_name, name, email, password]):
            flash('All fields are required.')
            return render_template('register.html')

        if User.query.filter_by(email=email).first():
            flash('An account with that email already exists.')
            return render_template('register.html')

        slug = company_name.lower().replace(' ', '-').replace("'", '')
        base_slug = slug
        counter = 1
        while Company.query.filter_by(slug=slug).first():
            slug = f"{base_slug}-{counter}"
            counter += 1

        from datetime import timedelta
        company = Company(
            name=company_name, slug=slug,
            subscription_status='trialing',
            trial_ends_at=datetime.utcnow() + timedelta(days=TRIAL_DAYS),
            trade_type=_bounded(trade_type, 100),
            acquisition_source=attribution.get('source'),
            acquisition_medium=attribution.get('medium'),
            acquisition_campaign=attribution.get('campaign'),
            acquisition_content=attribution.get('content'),
            acquisition_landing=attribution.get('landing'),
        )
        db.session.add(company)
        db.session.flush()

        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        user = User(company_id=company.id, email=email, name=name,
                    password_hash=pw_hash, role='owner')
        db.session.add(user)
        db.session.commit()

        login_user(user)
        _record_marketing_event(
            'registration_completed',
            company_id=company.id,
            details={'trade_type': company.trade_type or 'not_provided'},
            once=True,
        )
        if session.get('requested_plan') == 'founding_annual':
            return redirect(url_for('settings', plan='founding_annual'))
        return redirect(url_for('index'))

    _record_marketing_event('registration_started', once=True)
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user     = User.query.filter_by(email=email, is_active=True).first()

        if user and bcrypt.checkpw(password.encode(), user.password_hash):
            login_user(user)
            if user.role == 'employee':
                return redirect(url_for('employee_dashboard'))
            _record_marketing_event(
                'owner_login',
                company_id=user.company_id,
                once=True,
            )
            return redirect(url_for('index'))

        flash('Invalid email or password.')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ─────────────────────────────────────────
# OWNER ROUTES
# ─────────────────────────────────────────

@app.route('/')
def index():
    if not current_user.is_authenticated:
        _capture_attribution()
        _record_marketing_event('landing_view', once=True)
        return render_template('landing.html')
    if current_user.role == 'employee':
        return redirect(url_for('employee_dashboard'))
    company = Company.query.get(current_user.company_id)
    if company and not _has_active_access(company):
        return redirect(url_for('billing'))
    from datetime import date
    jobs        = Job.query.filter_by(company_id=current_user.company_id).order_by(Job.start_time).all()
    conflicts   = detect_conflicts(current_user.company_id)
    today       = [j for j in jobs if j.start_time.date() == date.today()]
    overdue      = [j for j in jobs if not j.invoice_sent and j.status == 'complete']
    unconfirmed  = [j for j in jobs if j.tech_assigned and not j.tech_confirmed and j.status == 'scheduled']
    active       = [j for j in jobs if j.status == 'in_progress']
    needs_review = [j for j in jobs if j.status == 'awaiting_review']
    employee_count = User.query.filter_by(
        company_id=current_user.company_id,
        role='employee',
        is_active=True,
    ).count()
    onboarding = {
        'employee_added': employee_count > 0,
        'job_created': len(jobs) > 0,
        'payouts_connected': bool(company.connect_charges_enabled),
        'subscribed': company.subscription_status == 'active',
    }
    onboarding_completed = sum(1 for value in onboarding.values() if value)
    return render_template('index.html',
        jobs=jobs, conflicts=conflicts, overdue=overdue,
        today=today, unconfirmed=unconfirmed, active=active,
        needs_review=needs_review, company=current_user.company,
        onboarding=onboarding,
        onboarding_completed=onboarding_completed,
        json_list=_json_list)


@app.route('/for/<slug>')
def segment_page(slug):
    page = SEGMENT_PAGES.get(slug)
    if not page:
        abort(404)
    _capture_attribution()
    _record_marketing_event(
        'segment_page_view',
        details={'segment': page['source']},
        once=True,
    )
    return render_template(
        'marketing_page.html',
        page=page,
        canonical_url=request.url_root.rstrip('/') + request.path,
    )


@app.route('/guides/<slug>')
def guide_page(slug):
    page = GUIDES.get(slug)
    if not page:
        abort(404)
    _capture_attribution()
    _record_marketing_event(
        'guide_view',
        details={'guide': page['source']},
        once=True,
    )
    return render_template(
        'marketing_page.html',
        page=page,
        canonical_url=request.url_root.rstrip('/') + request.path,
    )


@app.route('/tools/double-booking-cost-calculator')
def double_booking_calculator():
    _capture_attribution()
    _record_marketing_event('double_booking_calculator_view', once=True)
    return render_template(
        'double_booking_calculator.html',
        canonical_url=request.url_root.rstrip('/') + request.path,
    )


@app.route('/templates/field-job-template')
def field_job_template():
    _capture_attribution()
    _record_marketing_event('field_job_template_view', once=True)
    fields = [
        'Job ID', 'Source', 'Client', 'Location', 'Start', 'End',
        'Assigned technician', 'Status', 'Scope', 'Job value',
        'Technician pay', 'Closeout notes', 'Invoice sent', 'Payment received',
    ]
    return render_template(
        'job_template.html',
        fields=fields,
        canonical_url=request.url_root.rstrip('/') + request.path,
    )


@app.route('/templates/field-job-template.csv')
def download_field_job_template():
    _capture_attribution()
    _record_marketing_event('field_job_template_downloaded', once=True)
    headers = (
        'Job ID,Source,Client,Location,Start,End,Assigned technician,Status,'
        'Scope,Job value,Technician pay,Closeout notes,Invoice sent,Payment received\n'
    )
    response = app.response_class(headers, mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename=field-service-job-template.csv'
    return response


@app.route('/privacy')
def privacy():
    sections = [
        ('What FieldBase processes', [
            'FieldBase processes account and company information, jobs, schedules, technician assignments, notes, uploaded documents and photos, invoice and payment-status information, and basic first-party product usage events.',
            'FieldBase analytics records bounded event names and acquisition fields. It does not intentionally copy customer job descriptions, notes, photos, documents, passwords, or payment-card numbers into the marketing event table.',
        ]),
        ('Why the data is used', [
            'Data is used to provide the requested scheduling, dispatch, field documentation, invoicing, payment, account security, support, and product-improvement functions.',
        ]),
        ('Service providers', [
            'FieldBase uses infrastructure and payment providers, including Railway and Stripe. Storage, email, marketplace, and AI extraction providers are used only when the related feature is configured or invoked.',
            'Payment-card details are collected by Stripe rather than stored directly by FieldBase.',
        ]),
        ('Customer choices', [
            'Account owners control the job and crew information they enter, the integrations they configure, and whether they submit images or voice recordings for extraction. Owners are responsible for having authority to enter employee and client information.',
        ]),
        ('Retention and security', [
            'FieldBase retains operational information while needed to provide the account and for legitimate security, backup, billing, and legal purposes. Reasonable safeguards are used, but no internet service can guarantee absolute security.',
        ]),
    ]
    return render_template(
        'legal.html',
        title='Privacy notice',
        description='How FieldBase processes account, job, billing, and product usage information.',
        sections=sections,
        support_email=os.environ.get('FIELD_BASE_SUPPORT_EMAIL'),
        canonical_url=request.url_root.rstrip('/') + request.path,
    )


@app.route('/terms')
def terms():
    sections = [
        ('Using FieldBase', [
            'You must provide accurate account information, protect login credentials, and use FieldBase only for lawful field-service operations. Account owners are responsible for the people they invite and the data entered through their company account.',
        ]),
        ('Trials, subscriptions, and cancellation', [
            'The checkout page shows the current price, billing interval, and renewal terms before purchase. A trial does not require a card unless the checkout page explicitly says otherwise.',
            'Active subscriptions can be managed or canceled through the Stripe billing portal in FieldBase settings. Cancellation stops future renewal; access may continue through the paid billing period. Refund requests are reviewed case by case, subject to applicable law.',
        ]),
        ('External services', [
            'Stripe, marketplace APIs, storage, email, maps, and AI-assisted extraction are external services. Their availability and separate terms can affect the corresponding FieldBase feature.',
        ]),
        ('Operational responsibility', [
            'FieldBase supports scheduling, documentation, invoicing, and payment tracking. Customers remain responsible for job acceptance, staffing, safety, licensing, client approval, invoice accuracy, taxes, and regulatory obligations.',
        ]),
        ('Availability and changes', [
            'FieldBase may change or discontinue features to maintain security, reliability, or product fit. Material subscription changes will be presented through the product or account communications when reasonably possible.',
        ]),
    ]
    return render_template(
        'legal.html',
        title='Terms of service',
        description='Operational terms for FieldBase trials, subscriptions, accounts, and external services.',
        sections=sections,
        support_email=os.environ.get('FIELD_BASE_SUPPORT_EMAIL'),
        canonical_url=request.url_root.rstrip('/') + request.path,
    )


@app.route('/robots.txt')
def robots_txt():
    body = 'User-agent: *\nAllow: /\nSitemap: ' + request.url_root.rstrip('/') + '/sitemap.xml\n'
    return app.response_class(body, mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap_xml():
    paths = [
        '/',
        '/for/workmarket-contractors',
        '/for/field-nation-contractors',
        '/for/low-voltage-contractors',
        '/for/it-field-service',
        '/for/security-camera-installers',
        '/for/pos-installers',
        '/guides/field-service-invoicing',
        '/guides/prevent-double-booking-field-technicians',
        '/tools/double-booking-cost-calculator',
        '/templates/field-job-template',
        '/privacy',
        '/terms',
    ]
    root = request.url_root.rstrip('/')
    urls = ''.join(f'<url><loc>{root}{path}</loc></url>' for path in paths)
    xml = '<?xml version="1.0" encoding="UTF-8"?>' \
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + urls + '</urlset>'
    return app.response_class(xml, mimetype='application/xml')


@app.route('/calendar')
@login_required
@owner_required
def calendar():
    jobs      = Job.query.filter_by(company_id=current_user.company_id).all()
    conflicts = detect_conflicts(current_user.company_id)
    clients = Client.query.filter_by(company_id=current_user.company_id).order_by(Client.name).all()
    templates = JobTemplate.query.filter_by(company_id=current_user.company_id).order_by(JobTemplate.name).all()
    return render_template('calendar.html', jobs=jobs, conflicts=conflicts, clients=clients, templates=templates)


@app.route('/job-brief')
@login_required
@owner_required
def job_brief():
    employees = User.query.filter_by(
        company_id=current_user.company_id,
        role='employee',
        is_active=True
    ).order_by(User.name).all()
    std = TechStandard.query.filter_by(company_id=current_user.company_id).first()
    return render_template('job_brief.html', owner=current_user, employees=employees, std=std)


def _json_list(raw):
    try:
        value = json.loads(raw or '[]')
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def _next_invoice_number(company_id):
    latest = InvoiceRecord.query.filter_by(company_id=company_id).order_by(
        InvoiceRecord.id.desc()
    ).first()
    sequence = (latest.id + 1) if latest else 1
    return f'INV-{datetime.utcnow().strftime("%Y")}-{sequence:04d}'


def _create_invoice_for_job(job, status='draft'):
    existing = InvoiceRecord.query.filter_by(
        company_id=job.company_id,
        job_id=job.id,
    ).order_by(InvoiceRecord.id.desc()).first()
    if existing:
        return existing
    amount = round(float(job.job_pay or 0), 2)
    invoice = InvoiceRecord(
        company_id=job.company_id,
        job_id=job.id,
        client_id=job.client_id,
        number=_next_invoice_number(job.company_id),
        status=status,
        issue_date=datetime.utcnow(),
        due_date=datetime.utcnow() + timedelta(days=30),
        client_name=job.client_name,
        client_company=job.client_company,
        client_email=job.client_email,
        line_items=json.dumps([{
            'description': job.title,
            'quantity': 1,
            'unit_price': amount,
            'total': amount,
        }]),
        subtotal=amount,
        total=amount,
        notes=f'Job completed at {job.location}' if job.location else None,
    )
    db.session.add(invoice)
    db.session.flush()
    return invoice


def _invoice_checkout(invoice):
    if invoice.total <= 0:
        raise ValueError('Invoice total must be greater than zero.')
    company = db.session.get(Company, invoice.company_id)
    request_options = {}
    if company and company.connect_charges_enabled and company.stripe_connect_id:
        request_options['stripe_account'] = company.stripe_connect_id
    session_obj = stripe.checkout.Session.create(
        mode='payment',
        line_items=[{
            'price_data': {
                'currency': 'usd',
                'product_data': {
                    'name': f'Invoice {invoice.number}',
                    'description': (invoice.client_company or invoice.client_name or '')[:200],
                },
                'unit_amount': int(round(invoice.total * 100)),
            },
            'quantity': 1,
        }],
        customer_email=invoice.client_email or None,
        metadata={
            'invoice_id': str(invoice.id),
            'job_id': str(invoice.job_id or ''),
            'company_id': str(invoice.company_id),
        },
        success_url=request.host_url.rstrip('/') + '/invoice/payment/success',
        cancel_url=request.host_url.rstrip('/') + '/invoice/payment/canceled',
        **request_options,
    )
    invoice.stripe_checkout_session_id = session_obj.id
    invoice.stripe_checkout_url = session_obj.url
    db.session.commit()
    return session_obj.url


def _send_invoice_record(invoice, reminder=False):
    if not invoice.client_email:
        return False
    if not invoice.stripe_checkout_url:
        _invoice_checkout(invoice)
    owner = User.query.filter_by(
        company_id=invoice.company_id,
        role='owner',
        is_active=True,
    ).first()
    company = db.session.get(Company, invoice.company_id)
    client_name = html_lib.escape(invoice.client_name or 'Valued Client')
    company_name = html_lib.escape(company.name if company else 'Your Service Provider')
    owner_name = html_lib.escape(owner.name if owner else '')
    invoice_number = html_lib.escape(invoice.number)
    amount = f'${invoice.total:,.2f}'
    due = invoice.due_date.strftime('%B %d, %Y') if invoice.due_date else 'Due on receipt'
    intro = (
        f'This is a reminder that invoice <strong>{invoice_number}</strong> is still outstanding.'
        if reminder else
        f'Invoice <strong>{invoice_number}</strong> is ready for payment.'
    )
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;">
      <div style="background:#17324f;padding:24px 32px;color:#fff;">
        <h1 style="margin:0;font-size:22px;">{company_name}</h1>
        <p style="margin:5px 0 0;color:#bfdbfe;">{invoice_number}</p>
      </div>
      <div style="padding:32px;">
        <p>Dear {client_name},</p>
        <p>{intro}</p>
        <div style="background:#f8fafc;border-radius:8px;padding:18px;margin:22px 0;">
          <div style="font-size:13px;color:#64748b;">Amount Due</div>
          <div style="font-size:28px;font-weight:800;color:#17324f;">{amount}</div>
          <div style="font-size:13px;color:#64748b;margin-top:4px;">Due {due}</div>
        </div>
        <a href="{html_lib.escape(invoice.stripe_checkout_url)}" style="display:inline-block;background:#2563eb;color:#fff;padding:13px 24px;border-radius:8px;text-decoration:none;font-weight:700;">Pay Securely</a>
        <p style="margin-top:26px;color:#475569;">— {owner_name}<br>{company_name}</p>
      </div>
    </div>"""
    sent = send_email(
        invoice.client_email,
        f'{"Reminder: " if reminder else ""}Invoice {invoice.number} — {amount}',
        html,
    )
    if sent:
        invoice.status = 'sent' if invoice.status == 'draft' else invoice.status
        invoice.sent_at = invoice.sent_at or datetime.utcnow()
        if reminder:
            invoice.reminder_sent_at = datetime.utcnow()
        if invoice.job_id:
            job = db.session.get(Job, invoice.job_id)
            if job:
                job.invoice_sent = True
                job.invoice_sent_at = job.invoice_sent_at or datetime.utcnow()
                job.stripe_payment_link = invoice.stripe_checkout_url
        db.session.commit()
    return sent


@app.route('/invoice')
@app.route('/invoices')
@login_required
@owner_required
def invoice():
    invoices = InvoiceRecord.query.filter_by(
        company_id=current_user.company_id
    ).order_by(InvoiceRecord.created_at.desc()).all()
    jobs = Job.query.filter(
        Job.company_id == current_user.company_id,
        Job.job_pay.isnot(None),
    ).order_by(Job.start_time.desc()).all()
    clients = Client.query.filter_by(
        company_id=current_user.company_id
    ).order_by(Client.name).all()
    today = datetime.utcnow()
    for item in invoices:
        if item.status == 'sent' and item.due_date and item.due_date < today:
            item.status = 'overdue'
    db.session.commit()
    return render_template(
        'invoice.html',
        invoices=invoices,
        jobs=jobs,
        clients=clients,
        outstanding=sum(max(0, item.total - (item.amount_paid or 0)) for item in invoices if item.status not in ('paid', 'void')),
        overdue_count=sum(1 for item in invoices if item.status == 'overdue'),
        paid_total=sum(item.amount_paid or 0 for item in invoices if item.status == 'paid'),
        json_list=_json_list,
    )


@app.route('/api/invoices', methods=['POST'])
@login_required
@owner_required
def create_invoice():
    data = request.get_json(silent=True) or {}
    job = None
    client = None
    if data.get('job_id'):
        job = Job.query.filter_by(
            id=data.get('job_id'),
            company_id=current_user.company_id,
        ).first_or_404()
    if data.get('client_id'):
        client = Client.query.filter_by(
            id=data.get('client_id'),
            company_id=current_user.company_id,
        ).first_or_404()
    if job:
        invoice = _create_invoice_for_job(job)
    else:
        description = _bounded(data.get('description'), 300)
        unit_price = _to_float(data.get('unit_price'))
        quantity = _to_float(data.get('quantity')) or 1
        tax_rate = max(0, min(_to_float(data.get('tax_rate')) or 0, 100))
        if not description or unit_price is None or unit_price <= 0:
            return jsonify({'error': 'Description and a positive amount are required.'}), 400
        subtotal = round(quantity * unit_price, 2)
        tax_amount = round(subtotal * tax_rate / 100, 2)
        due_days = max(0, min(_to_int(data.get('due_days'), 30), 365))
        invoice = InvoiceRecord(
            company_id=current_user.company_id,
            client_id=client.id if client else None,
            number=_next_invoice_number(current_user.company_id),
            due_date=datetime.utcnow() + timedelta(days=due_days),
            client_name=client.name if client else _bounded(data.get('client_name'), 200),
            client_company=client.company_name if client else _bounded(data.get('client_company'), 200),
            client_email=client.email if client else _bounded(data.get('client_email'), 200),
            line_items=json.dumps([{
                'description': description,
                'quantity': quantity,
                'unit_price': unit_price,
                'total': subtotal,
            }]),
            subtotal=subtotal,
            tax_rate=tax_rate,
            tax_amount=tax_amount,
            total=subtotal + tax_amount,
            notes=_bounded(data.get('notes'), 2000),
        )
        db.session.add(invoice)
    db.session.commit()
    return jsonify({'success': True, 'id': invoice.id, 'number': invoice.number})


@app.route('/api/invoices/<int:invoice_id>/send', methods=['POST'])
@login_required
@owner_required
def send_invoice(invoice_id):
    invoice = InvoiceRecord.query.filter_by(
        id=invoice_id,
        company_id=current_user.company_id,
    ).first_or_404()
    if not invoice.client_email:
        return jsonify({'error': 'Add a client email before sending.'}), 400
    try:
        sent = _send_invoice_record(invoice)
    except Exception:
        app.logger.exception('Invoice delivery failed')
        return jsonify({'error': 'Could not create the secure payment page.'}), 502
    if not sent:
        return jsonify({'error': 'Email is not configured or delivery failed.'}), 503
    _record_marketing_event('first_invoice_sent', company_id=current_user.company_id, once=True)
    return jsonify({'success': True, 'checkout_url': invoice.stripe_checkout_url})


@app.route('/api/invoices/<int:invoice_id>/remind', methods=['POST'])
@login_required
@owner_required
def remind_invoice(invoice_id):
    invoice = InvoiceRecord.query.filter_by(
        id=invoice_id,
        company_id=current_user.company_id,
    ).first_or_404()
    if invoice.status == 'paid':
        return jsonify({'error': 'This invoice is already paid.'}), 400
    try:
        sent = _send_invoice_record(invoice, reminder=True)
    except Exception:
        app.logger.exception('Invoice reminder failed')
        return jsonify({'error': 'Could not send the payment reminder.'}), 502
    return jsonify({'success': sent}) if sent else (jsonify({'error': 'Email delivery failed.'}), 503)


@app.route('/api/invoices/<int:invoice_id>/paid', methods=['POST'])
@login_required
@owner_required
def mark_invoice_paid(invoice_id):
    invoice = InvoiceRecord.query.filter_by(
        id=invoice_id,
        company_id=current_user.company_id,
    ).first_or_404()
    data = request.get_json(silent=True) or {}
    amount = _to_float(data.get('amount'))
    invoice.amount_paid = invoice.total if amount is None else max(0, min(amount, invoice.total))
    invoice.status = 'paid' if invoice.amount_paid >= invoice.total else 'partial'
    invoice.paid_at = datetime.utcnow() if invoice.status == 'paid' else None
    if invoice.job_id:
        job = db.session.get(Job, invoice.job_id)
        if job:
            job.payment_received = invoice.status == 'paid'
            job.amount_paid = invoice.amount_paid
    db.session.commit()
    _record_marketing_event('first_payment_recorded', company_id=current_user.company_id, once=True)
    return jsonify({'success': True, 'status': invoice.status})


@app.route('/invoice/payment/success')
def invoice_payment_success():
    return render_template('payment_result.html', paid=True)


@app.route('/invoice/payment/canceled')
def invoice_payment_canceled():
    return render_template('payment_result.html', paid=False)


@app.route('/clients', methods=['GET', 'POST'])
@login_required
@owner_required
def clients():
    if request.method == 'POST':
        name = _bounded(request.form.get('name'), 200)
        if not name:
            flash('Client name is required.')
            return redirect(url_for('clients'))
        email = _bounded(request.form.get('email'), 200)
        existing = Client.query.filter_by(
            company_id=current_user.company_id,
            email=email,
        ).first() if email else None
        if existing:
            flash('A client with that email already exists.')
            return redirect(url_for('clients'))
        db.session.add(Client(
            company_id=current_user.company_id,
            name=name,
            company_name=_bounded(request.form.get('company_name'), 200),
            email=email,
            phone=_bounded(request.form.get('phone'), 50),
            address=_bounded(request.form.get('address'), 300),
            billing_terms=_bounded(request.form.get('billing_terms'), 100) or 'Net 30',
            access_notes=_bounded(request.form.get('access_notes'), 2000),
        ))
        db.session.commit()
        flash('Client added.')
        return redirect(url_for('clients'))
    client_rows = Client.query.filter_by(
        company_id=current_user.company_id
    ).order_by(Client.name).all()
    job_counts = {
        client.id: Job.query.filter_by(
            company_id=current_user.company_id,
            client_id=client.id,
        ).count()
        for client in client_rows
    }
    return render_template('clients.html', clients=client_rows, job_counts=job_counts)


@app.route('/api/clients/<int:client_id>', methods=['DELETE'])
@login_required
@owner_required
def delete_client(client_id):
    client = Client.query.filter_by(
        id=client_id,
        company_id=current_user.company_id,
    ).first_or_404()
    Job.query.filter_by(company_id=current_user.company_id, client_id=client.id).update({'client_id': None})
    db.session.delete(client)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/job-templates', methods=['GET', 'POST'])
@login_required
@owner_required
def job_templates():
    if request.method == 'POST':
        name = _bounded(request.form.get('name'), 200)
        title = _bounded(request.form.get('title'), 200)
        if not name or not title:
            flash('Template name and job title are required.')
            return redirect(url_for('job_templates'))
        checklist = [
            item.strip()[:200]
            for item in (request.form.get('checklist') or '').splitlines()
            if item.strip()
        ][:20]
        db.session.add(JobTemplate(
            company_id=current_user.company_id,
            name=name,
            title=title,
            platform=_bounded(request.form.get('platform'), 50) or 'manual',
            duration_minutes=max(15, min(_to_int(request.form.get('duration_minutes'), 120), 1440)),
            default_tech_pay=_to_float(request.form.get('default_tech_pay')),
            default_job_pay=_to_float(request.form.get('default_job_pay')),
            notes=_bounded(request.form.get('notes'), 3000),
            checklist=json.dumps(checklist),
            require_signature=request.form.get('require_signature') == 'on',
        ))
        db.session.commit()
        flash('Job template saved.')
        return redirect(url_for('job_templates'))
    templates = JobTemplate.query.filter_by(
        company_id=current_user.company_id
    ).order_by(JobTemplate.name).all()
    return render_template('job_templates.html', templates=templates, json_list=_json_list)


@app.route('/api/job-templates/<int:template_id>', methods=['GET', 'DELETE'])
@login_required
@owner_required
def job_template_api(template_id):
    template = JobTemplate.query.filter_by(
        id=template_id,
        company_id=current_user.company_id,
    ).first_or_404()
    if request.method == 'DELETE':
        db.session.delete(template)
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({
        'id': template.id,
        'name': template.name,
        'title': template.title,
        'platform': template.platform,
        'duration_minutes': template.duration_minutes,
        'tech_pay': template.default_tech_pay,
        'job_pay': template.default_job_pay,
        'notes': template.notes,
        'checklist': _json_list(template.checklist),
        'require_signature': template.require_signature,
    })

# ─────────────────────────────────────────
# EMPLOYEE ROUTES
# ─────────────────────────────────────────

@app.route('/employee')
@login_required
def employee_dashboard():
    if current_user.role == 'owner':
        return redirect(url_for('index'))
    jobs = Job.query.filter_by(
        company_id=current_user.company_id,
        tech_assigned=current_user.name
    ).order_by(Job.start_time).all()
    return render_template('employee.html', jobs=jobs, json_list=_json_list)

# ─────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────

@app.route('/api/jobs')
@login_required
@owner_required
def get_jobs():
    jobs = Job.query.filter_by(company_id=current_user.company_id).all()
    return jsonify([{
        'id':        j.id,
        'title':     j.title,
        'platform':  j.platform,
        'location':  j.location,
        'start':     j.start_time.isoformat(),
        'end':       j.end_time.isoformat(),
        'status':    j.status,
        'tech':      j.tech_assigned,
        'confirmed': j.tech_confirmed,
        'tech_pay':  j.tech_pay,
        'job_pay':   j.job_pay,
        'notes':     j.notes,
        'client_name':    j.client_name,
        'client_company': j.client_company,
        'client_email':   j.client_email,
        'client_id':      j.client_id,
        'template_id':    j.job_template_id,
        'checklist':      _json_list(j.closeout_checklist),
        'signed':         bool(j.signed_at),
        'signature_name': j.signature_name,
        'color':     platform_color(j.platform)
    } for j in jobs])


def _to_float(value):
    """Coerce form/JSON values to float or None — empty strings and garbage never crash."""
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value, default=None):
    if value in (None, ''):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_dt(value):
    """Parse an ISO datetime string, returning None instead of raising."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _job_client_from_data(company_id, data):
    client_id = data.get('client_id')
    if client_id:
        return Client.query.filter_by(id=client_id, company_id=company_id).first()
    email = _bounded(data.get('client_email'), 200)
    name = _bounded(data.get('client_name'), 200)
    if not email and not name:
        return None
    client = Client.query.filter_by(company_id=company_id, email=email).first() if email else None
    if not client:
        client = Client(
            company_id=company_id,
            name=name or email,
            company_name=_bounded(data.get('client_company'), 200),
            email=email,
            address=_bounded(data.get('location'), 300),
        )
        db.session.add(client)
        db.session.flush()
    else:
        client.name = name or client.name
        client.company_name = _bounded(data.get('client_company'), 200) or client.company_name
        client.address = _bounded(data.get('location'), 300) or client.address
    return client


@app.route('/api/jobs', methods=['POST'])
@login_required
@owner_required
def add_job():
    try:
        data  = request.json or {}
        template = None
        if data.get('template_id'):
            template = JobTemplate.query.filter_by(
                id=data.get('template_id'),
                company_id=current_user.company_id,
            ).first_or_404()
            data = {
                'title': template.title,
                'platform': template.platform,
                'tech_pay': template.default_tech_pay,
                'job_pay': template.default_job_pay,
                'notes': template.notes,
                **data,
            }
        title = (data.get('title') or '').strip()
        if not title:
            return jsonify({'error': 'Job title is required.'}), 400
        start_time = _parse_dt(data.get('start'))
        end_time   = _parse_dt(data.get('end'))
        if not start_time or not end_time:
            return jsonify({'error': 'Valid start and end times are required.'}), 400
        if end_time <= start_time:
            return jsonify({'error': 'End time must be after start time.'}), 400

        client = _job_client_from_data(current_user.company_id, data)
        checklist = [
            {'label': item, 'done': False}
            for item in _json_list(template.checklist if template else '[]')
        ]
        job = Job(
            company_id    = current_user.company_id,
            title         = title,
            platform      = data.get('platform', 'manual'),
            location      = data.get('location', ''),
            start_time    = start_time,
            end_time      = end_time,
            tech_assigned = data.get('tech', ''),
            tech_pay      = _to_float(data.get('tech_pay')),
            job_pay       = _to_float(data.get('job_pay')),
            notes          = data.get('notes', ''),
            client_name    = client.name if client else data.get('client_name', ''),
            client_company = client.company_name if client else data.get('client_company', ''),
            client_email   = client.email if client else data.get('client_email', ''),
            client_id      = client.id if client else None,
            job_template_id = template.id if template else None,
            closeout_checklist = json.dumps(checklist),
            signature_required = bool(template and template.require_signature),
        )
        if job.location:
            job.job_lat, job.job_lng = _geocode_address(job.location)
        db.session.add(job)
        created_jobs = [job]
        repeat = data.get('repeat')
        repeat_count = max(1, min(_to_int(data.get('repeat_count'), 1), 52))
        if repeat in ('weekly', 'monthly') and repeat_count > 1:
            for occurrence in range(1, repeat_count):
                offset = (
                    timedelta(weeks=occurrence)
                    if repeat == 'weekly'
                    else relativedelta(months=occurrence)
                )
                repeated = Job(
                    company_id=job.company_id,
                    title=job.title,
                    platform=job.platform,
                    location=job.location,
                    start_time=job.start_time + offset,
                    end_time=job.end_time + offset,
                    tech_assigned=job.tech_assigned,
                    tech_pay=job.tech_pay,
                    job_pay=job.job_pay,
                    notes=job.notes,
                    client_name=job.client_name,
                    client_company=job.client_company,
                    client_email=job.client_email,
                    client_id=job.client_id,
                    job_template_id=job.job_template_id,
                    closeout_checklist=job.closeout_checklist,
                    signature_required=job.signature_required,
                    job_lat=job.job_lat,
                    job_lng=job.job_lng,
                )
                db.session.add(repeated)
                created_jobs.append(repeated)
        db.session.commit()
        _record_marketing_event(
            'first_job_created',
            company_id=current_user.company_id,
            details={
                'entry_method': 'manual',
                'assigned': bool(job.tech_assigned),
                'has_client_email': bool(job.client_email),
            },
            once=True,
        )
        if job.tech_assigned:
            _record_marketing_event(
                'first_job_assigned',
                company_id=current_user.company_id,
                once=True,
            )
        try:
            detect_and_save_conflicts(current_user.company_id)
        except Exception as ce:
            app.logger.error(f'Conflict detection failed: {ce}')

        # Notify assigned employee by email.
        # The job is already committed — a notification failure must never
        # bubble up as a 500, or the UI reports an error for a saved job.
        try:
            _notify_assigned_employee(job)
        except Exception as ne:
            app.logger.error(f'Employee notification failed: {ne}')

        return jsonify({
            'success': True,
            'id': job.id,
            'ids': [created.id for created in created_jobs],
            'created_count': len(created_jobs),
        })
    except Exception as e:
        db.session.rollback()
        app.logger.exception('add_job failed')
        return jsonify({'error': 'Could not save the job. Please try again.'}), 500


def _notify_assigned_employee(job):
        if job.tech_assigned:
            emp = User.query.filter_by(
                company_id=job.company_id,
                name=job.tech_assigned,
                role='employee'
            ).first()
            if emp:
                html = f"""
                <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
                  <div style="background:#1e3a5f;padding:24px 32px;">
                    <h1 style="color:#fff;margin:0;font-size:20px;">New Job Assigned</h1>
                    <p style="color:#a8c4e0;margin:4px 0 0;">{current_user.company.name}</p>
                  </div>
                  <div style="padding:32px;">
                    <p style="color:#374151;">Hi {emp.name}, you have been assigned a new job.</p>
                    <table style="width:100%;border-collapse:collapse;margin:20px 0;">
                      <tr style="background:#f9fafb;"><td style="padding:10px 14px;font-size:13px;font-weight:600;color:#6b7280;">Job</td><td style="padding:10px 14px;font-size:14px;color:#1f2937;">{job.title}</td></tr>
                      <tr><td style="padding:10px 14px;font-size:13px;font-weight:600;color:#6b7280;">Date</td><td style="padding:10px 14px;font-size:14px;color:#1f2937;">{job.start_time.strftime('%A, %B %d at %I:%M %p')}</td></tr>
                      <tr style="background:#f9fafb;"><td style="padding:10px 14px;font-size:13px;font-weight:600;color:#6b7280;">Location</td><td style="padding:10px 14px;font-size:14px;color:#1f2937;">{job.location or 'TBD'}</td></tr>
                      {"<tr><td style='padding:10px 14px;font-size:13px;font-weight:600;color:#6b7280;'>Pay</td><td style='padding:10px 14px;font-size:14px;font-weight:700;color:#166534;'>$" + f"{job.tech_pay:.2f}" + "</td></tr>" if job.tech_pay else ""}
                    </table>
                    <p style="color:#374151;">Please log in to confirm this job.</p>
                  </div>
                </div>"""
                send_email(emp.email, f'New Job: {job.title}', html)


@app.route('/api/jobs/<int:job_id>', methods=['PUT'])
@login_required
@owner_required
def update_job(job_id):
    job = Job.query.filter_by(id=job_id, company_id=current_user.company_id).first_or_404()
    try:
        data = request.json or {}

        title = (data.get('title') or '').strip()
        if not title:
            return jsonify({'error': 'Job title is required.'}), 400
        start_time = _parse_dt(data.get('start'))
        end_time   = _parse_dt(data.get('end'))
        if not start_time or not end_time:
            return jsonify({'error': 'Valid start and end times are required.'}), 400
        if end_time <= start_time:
            return jsonify({'error': 'End time must be after start time.'}), 400

        old_tech     = job.tech_assigned or ''
        old_location = job.location or ''
        client = _job_client_from_data(current_user.company_id, data)

        job.title          = title
        job.platform       = data.get('platform', job.platform)
        job.location       = data.get('location', '')
        job.start_time     = start_time
        job.end_time       = end_time
        job.tech_assigned  = data.get('tech', '')
        job.tech_pay       = _to_float(data.get('tech_pay'))
        job.job_pay        = _to_float(data.get('job_pay'))
        job.notes          = data.get('notes', '')
        job.client_name    = client.name if client else data.get('client_name', '')
        job.client_company = client.company_name if client else data.get('client_company', '')
        job.client_email   = client.email if client else data.get('client_email', '')
        job.client_id      = client.id if client else None

        new_status = data.get('status')
        if new_status and new_status != job.status:
            allowed_statuses = {'scheduled', 'on_the_way', 'delayed', 'in_progress', 'paused', 'complete', 'awaiting_review'}
            if new_status not in allowed_statuses:
                return jsonify({'error': 'Invalid status.'}), 400
            job.status = new_status
            if new_status == 'complete' and not job.completed_at:
                job.completed_at = datetime.utcnow()

        # Reassignment means the new employee must confirm fresh
        if (job.tech_assigned or '') != old_tech:
            job.tech_confirmed = False
            job.confirmed_at   = None

        if (job.location or '') != old_location:
            job.job_lat, job.job_lng = _geocode_address(job.location) if job.location else (None, None)

        db.session.commit()

        try:
            detect_and_save_conflicts(current_user.company_id)
        except Exception as ce:
            app.logger.error(f'Conflict detection failed: {ce}')

        if job.tech_assigned and (job.tech_assigned or '') != old_tech:
            try:
                _notify_assigned_employee(job)
            except Exception as ne:
                app.logger.error(f'Employee notification failed: {ne}')

        return jsonify({'success': True, 'id': job.id})
    except Exception as e:
        db.session.rollback()
        app.logger.exception('update_job failed')
        return jsonify({'error': 'Could not update the job. Please try again.'}), 500


@app.route('/api/jobs/<int:job_id>', methods=['DELETE'])
@login_required
@owner_required
def delete_job(job_id):
    job = Job.query.filter_by(id=job_id, company_id=current_user.company_id).first_or_404()
    Conflict.query.filter(
        (Conflict.job_a_id == job_id) | (Conflict.job_b_id == job_id)
    ).delete(synchronize_session=False)
    for doc in JobDocument.query.filter_by(job_id=job_id).all():
        storage.delete('docs', doc.filename)
        db.session.delete(doc)
    for photo in JobPhoto.query.filter_by(job_id=job_id).all():
        storage.delete('photos', photo.filename)
        db.session.delete(photo)
    db.session.delete(job)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/conflicts')
@login_required
@owner_required
def get_conflicts():
    return jsonify(detect_conflicts(current_user.company_id))


@app.route('/api/invoice/<int:job_id>/sent', methods=['POST'])
@login_required
@owner_required
def mark_invoice_sent(job_id):
    job = Job.query.filter_by(id=job_id, company_id=current_user.company_id).first_or_404()
    job.invoice_sent     = True
    job.invoice_sent_at  = datetime.utcnow()
    db.session.commit()
    _record_marketing_event(
        'first_invoice_sent',
        company_id=current_user.company_id,
        once=True,
    )
    return jsonify({'success': True})


@app.route('/api/jobs/<int:job_id>/payment', methods=['POST'])
@login_required
@owner_required
def update_payment(job_id):
    job = Job.query.filter_by(id=job_id, company_id=current_user.company_id).first_or_404()
    data = request.json
    job.payment_received = data.get('payment_received', False)
    job.amount_paid      = data.get('amount_paid')
    db.session.commit()
    if job.payment_received:
        _record_marketing_event(
            'first_payment_recorded',
            company_id=current_user.company_id,
            once=True,
        )
    return jsonify({'success': True})

# ─────────────────────────────────────────
# TEAM ROUTES
# ─────────────────────────────────────────

@app.route('/team', methods=['GET', 'POST'])
@login_required
@owner_required
def team():
    if request.method == 'POST':
        name        = request.form.get('name', '').strip()
        email       = request.form.get('email', '').strip().lower()
        password    = request.form.get('password', '')
        hourly_rate = request.form.get('hourly_rate', '').strip()

        if not all([name, email, password]):
            flash('All fields are required.')
            return redirect(url_for('team'))

        if User.query.filter_by(email=email).first():
            flash('An account with that email already exists.')
            return redirect(url_for('team'))

        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        employee = User(
            company_id    = current_user.company_id,
            email         = email,
            name          = name,
            password_hash = pw_hash,
            role          = 'employee',
            hourly_rate   = float(hourly_rate) if hourly_rate else None
        )
        db.session.add(employee)
        db.session.commit()
        _record_marketing_event(
            'first_employee_added',
            company_id=current_user.company_id,
            once=True,
        )
        flash(f'{name} has been added to your team.')
        return redirect(url_for('team'))

    employees = User.query.filter_by(
        company_id=current_user.company_id,
        role='employee'
    ).order_by(User.created_at).all()
    return render_template('team.html', employees=employees, company=current_user.company)


@app.route('/team/<int:user_id>/deactivate', methods=['POST'])
@login_required
@owner_required
def deactivate_employee(user_id):
    employee = User.query.filter_by(id=user_id, company_id=current_user.company_id, role='employee').first_or_404()
    employee.is_active = not employee.is_active
    db.session.commit()
    return jsonify({'success': True, 'active': employee.is_active})


@app.route('/team/<int:user_id>/hourly-rate', methods=['POST'])
@login_required
@owner_required
def update_hourly_rate(user_id):
    employee = User.query.filter_by(id=user_id, company_id=current_user.company_id, role='employee').first_or_404()
    rate = request.json.get('hourly_rate')
    employee.hourly_rate = float(rate) if rate else None
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/employee/jobs')
@login_required
def employee_jobs_api():
    jobs = Job.query.filter_by(
        company_id=current_user.company_id,
        tech_assigned=current_user.name
    ).all()
    color_map = {'scheduled': '#f59e0b', 'confirmed': '#3b82f6', 'in_progress': '#8b5cf6', 'complete': '#22c55e', 'awaiting_review': '#f97316'}
    return jsonify([{
        'id':    j.id,
        'title': j.title,
        'start': j.start_time.isoformat(),
        'end':   j.end_time.isoformat(),
        'color': color_map.get(j.status, '#6b7280'),
        'extendedProps': {
            'location': j.location,
            'pay':      j.tech_pay,
            'status':   j.status,
        }
    } for j in jobs])


# ─────────────────────────────────────────
# EMPLOYEE API ROUTES
# ─────────────────────────────────────────

def _employee_job(job_id):
    """Get a job the current user may act on. Employees can only touch jobs
    assigned to them — without this, any employee could confirm or clock in
    to a teammate's job."""
    query = Job.query.filter_by(id=job_id, company_id=current_user.company_id)
    if current_user.role != 'owner':
        query = query.filter_by(tech_assigned=current_user.name)
    return query.first_or_404()

def _notify_owner(job, subject, message):
    owner = User.query.filter_by(company_id=job.company_id, role='owner').first()
    if owner and owner.email:
        html = f'<p style="font-family:sans-serif;font-size:15px;">{message}</p><p style="font-family:sans-serif;font-size:13px;color:#6b7280;">Job: <strong>{job.title}</strong></p>'
        send_email(owner.email, subject, html)


def _notify_client(job, event_type, headline, message):
    if not job.client_email:
        return False
    company = db.session.get(Company, job.company_id)
    if not company or not company.client_notifications_enabled:
        return False
    safe_title = html_lib.escape(job.title)
    safe_headline = html_lib.escape(headline)
    safe_message = html_lib.escape(message)
    company_name = html_lib.escape(company.name if company else 'Your Service Provider')
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;">
      <div style="background:#17324f;color:#fff;padding:22px 28px;">
        <div style="font-size:13px;color:#bfdbfe;">{company_name}</div>
        <h1 style="font-size:21px;margin:5px 0 0;">{safe_headline}</h1>
      </div>
      <div style="padding:28px;">
        <p style="font-size:15px;color:#334155;">{safe_message}</p>
        <div style="background:#f8fafc;padding:14px 16px;border-radius:8px;color:#475569;"><strong>{safe_title}</strong><br>{html_lib.escape(job.location or 'Location to be confirmed')}</div>
      </div>
    </div>"""
    sent = send_email(job.client_email, f'{headline} — {job.title}', html)
    db.session.add(DispatchEvent(
        company_id=job.company_id,
        job_id=job.id,
        event_type=event_type,
        recipient=job.client_email,
        status='sent' if sent else 'failed',
    ))
    db.session.commit()
    return sent

@app.route('/api/jobs/<int:job_id>/confirm', methods=['POST'])
@login_required
def confirm_job(job_id):
    job = _employee_job(job_id)
    job.tech_confirmed = True
    job.confirmed_at   = datetime.utcnow()
    db.session.commit()
    _notify_owner(job, f'Job Confirmed — {job.title}', f'{current_user.name} confirmed the job <strong>{job.title}</strong>.')
    return jsonify({'success': True})

@app.route('/api/jobs/<int:job_id>/on-the-way', methods=['POST'])
@login_required
def on_the_way(job_id):
    job = _employee_job(job_id)
    job.status = 'on_the_way'
    db.session.commit()
    _notify_owner(job, f'On the Way — {job.title}', f'{current_user.name} is on the way to <strong>{job.title}</strong>.')
    _notify_client(job, 'on_the_way', 'Your Technician Is on the Way', f'{current_user.name} is traveling to your service appointment.')
    return jsonify({'success': True})

@app.route('/api/jobs/<int:job_id>/delayed', methods=['POST'])
@login_required
def report_delayed(job_id):
    job = _employee_job(job_id)
    job.status = 'delayed'
    data = request.get_json(silent=True) or {}
    reason = data.get('reason', '').strip()
    db.session.commit()
    reason_line = f'<br>Reason: {reason}' if reason else ''
    _notify_owner(job, f'Delay Alert — {job.title}', f'{current_user.name} is delayed on <strong>{job.title}</strong>.{reason_line}')
    _notify_client(job, 'delayed', 'Service Update: Technician Delayed', f'{current_user.name} is delayed. {reason or "We will keep you updated."}')
    return jsonify({'success': True})

@app.route('/api/jobs/<int:job_id>/resume-travel', methods=['POST'])
@login_required
def resume_travel(job_id):
    job = _employee_job(job_id)
    job.status = 'on_the_way'
    db.session.commit()
    _notify_owner(job, f'En Route Again — {job.title}', f'{current_user.name} is back on the way to <strong>{job.title}</strong>.')
    _notify_client(job, 'on_the_way_again', 'Your Technician Is En Route Again', f'{current_user.name} has resumed travel to your appointment.')
    return jsonify({'success': True})

@app.route('/api/jobs/<int:job_id>/pause', methods=['POST'])
@login_required
def pause_job(job_id):
    job = _employee_job(job_id)
    job.status = 'paused'
    db.session.commit()
    _notify_owner(job, f'Job Paused — {job.title}', f'{current_user.name} paused work on <strong>{job.title}</strong>.')
    return jsonify({'success': True})

@app.route('/api/jobs/<int:job_id>/resume', methods=['POST'])
@login_required
def resume_job(job_id):
    job = _employee_job(job_id)
    job.status = 'in_progress'
    db.session.commit()
    _notify_owner(job, f'Job Resumed — {job.title}', f'{current_user.name} resumed work on <strong>{job.title}</strong>.')
    return jsonify({'success': True})

@app.route('/api/jobs/<int:job_id>/clock-in', methods=['POST'])
@login_required
def clock_in(job_id):
    job = _employee_job(job_id)
    job.clock_in_at = datetime.utcnow()
    job.status      = 'in_progress'
    data = request.get_json(silent=True) or {}
    emp_lat = data.get('lat')
    emp_lng = data.get('lng')
    if emp_lat and emp_lng:
        job.clock_in_lat = emp_lat
        job.clock_in_lng = emp_lng
    db.session.commit()

    loc_note = ' (GPS recorded)' if job.clock_in_lat else ''
    if emp_lat and emp_lng and job.job_lat and job.job_lng:
        dist = _haversine_miles(float(emp_lat), float(emp_lng), job.job_lat, job.job_lng)
        loc_note = f' ({dist:.1f} mi from site)'
        if dist > 0.5:
            _notify_owner(
                job,
                f'Location Alert — {current_user.name} clocked in {dist:.1f} mi from site',
                f'<p style="font-family:sans-serif;"><strong style="color:#dc2626;">Location Alert</strong></p>'
                f'<p style="font-family:sans-serif;">{current_user.name} clocked in on <strong>{job.title}</strong> '
                f'but is <strong>{dist:.1f} miles</strong> from the job site.</p>'
                f'<p style="font-family:sans-serif;color:#6b7280;">Job address: {job.location or "N/A"}</p>'
            )

    _notify_owner(job, f'Employee Clocked In — {job.title}', f'{current_user.name} clocked in on <strong>{job.title}</strong> at {job.clock_in_at.strftime("%I:%M %p")}{loc_note}.')
    _notify_client(job, 'arrived', 'Your Technician Has Arrived', f'{current_user.name} has arrived and started work.')
    return jsonify({'success': True})

@app.route('/api/jobs/<int:job_id>/clock-out', methods=['POST'])
@login_required
def clock_out(job_id):
    job = _employee_job(job_id)
    job.clock_out_at = datetime.utcnow()
    db.session.commit()
    _notify_owner(job, f'Employee Clocked Out — {job.title}', f'{current_user.name} clocked out of <strong>{job.title}</strong> at {job.clock_out_at.strftime("%I:%M %p")}.')
    return jsonify({'success': True})

def _send_auto_invoice(job):
    """Persist an invoice, create hosted Checkout, and email it to the client."""
    if not job.job_pay:
        return {
            'invoice_created': False,
            'invoice_sent': False,
            'warning': 'Add a job value before creating an invoice.',
        }
    try:
        invoice = _create_invoice_for_job(job)
        db.session.commit()
    except Exception as e:
        app.logger.error(f'Auto invoice creation failed for job {job.id}: {e}')
        return {
            'invoice_created': False,
            'invoice_sent': False,
            'warning': 'The job was closed, but the invoice could not be created.',
        }
    if not job.client_email:
        return {
            'invoice_created': True,
            'invoice_sent': False,
            'invoice_id': invoice.id,
            'warning': 'Invoice saved as a draft because the job has no client email.',
        }
    try:
        sent = _send_invoice_record(invoice)
        return {
            'invoice_created': True,
            'invoice_sent': bool(sent),
            'invoice_id': invoice.id,
            'warning': None if sent else 'Invoice saved as a draft because it could not be sent.',
        }
    except Exception as e:
        app.logger.error(f'Auto invoice failed for job {job.id}: {e}')
        return {
            'invoice_created': True,
            'invoice_sent': False,
            'invoice_id': invoice.id,
            'warning': 'Invoice saved as a draft because it could not be sent.',
        }


@app.route('/api/jobs/<int:job_id>/complete', methods=['POST'])
@login_required
def complete_job(job_id):
    job = _employee_job(job_id)
    checklist = _json_list(job.closeout_checklist)
    incomplete = [item for item in checklist if not item.get('done')]
    if incomplete:
        return jsonify({
            'error': f'Complete all {len(incomplete)} remaining closeout item{"s" if len(incomplete) != 1 else ""}.',
        }), 400
    if job.signature_required and not job.signed_at:
        return jsonify({'error': 'Collect the customer signature before completing this job.'}), 400
    job.status = 'awaiting_review'
    if not job.clock_out_at:
        job.clock_out_at = datetime.utcnow()
    db.session.commit()
    _record_marketing_event(
        'first_job_ready_for_review',
        company_id=job.company_id,
        once=True,
    )
    _notify_owner(job, f'Work Done — {job.title}', f'{current_user.name} finished <strong>{job.title}</strong>. Review and close when ready.')
    return jsonify({'success': True})


@app.route('/api/jobs/<int:job_id>/closeout', methods=['GET', 'PUT'])
@login_required
def job_closeout(job_id):
    job = _employee_job(job_id)
    if request.method == 'PUT':
        data = request.get_json(silent=True) or {}
        submitted = data.get('checklist')
        current = _json_list(job.closeout_checklist)
        if not isinstance(submitted, list) or len(submitted) != len(current):
            return jsonify({'error': 'Checklist does not match this job.'}), 400
        normalized = []
        for index, current_item in enumerate(current):
            normalized.append({
                'label': str(current_item.get('label') or '')[:200],
                'done': bool((submitted[index] or {}).get('done')),
            })
        job.closeout_checklist = json.dumps(normalized)
        db.session.commit()
    return jsonify({
        'checklist': _json_list(job.closeout_checklist),
        'signature_required': bool(job.signature_required),
        'signed': bool(job.signed_at),
        'signature_name': job.signature_name,
        'signed_at': job.signed_at.isoformat() if job.signed_at else None,
    })


@app.route('/api/jobs/<int:job_id>/signature', methods=['POST'])
@login_required
def save_job_signature(job_id):
    job = _employee_job(job_id)
    signer_name = _bounded(request.form.get('signer_name'), 200)
    signature = request.files.get('signature')
    if not signer_name or not signature:
        return jsonify({'error': 'Signer name and signature are required.'}), 400
    signature_bytes = signature.read(1_000_001)
    if len(signature_bytes) > 1_000_000:
        return jsonify({'error': 'Signature image is too large.'}), 413
    if not signature_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        return jsonify({'error': 'Signature must be a PNG image.'}), 400
    signature.seek(0)
    filename = f'{uuid.uuid4().hex}.png'
    storage.upload(signature, 'signatures', filename)
    if job.signature_filename:
        storage.delete('signatures', job.signature_filename)
    job.signature_name = signer_name
    job.signature_filename = filename
    job.signed_at = datetime.utcnow()
    db.session.commit()
    return jsonify({
        'success': True,
        'signature_name': job.signature_name,
        'signed_at': job.signed_at.isoformat(),
    })


@app.route('/api/jobs/<int:job_id>/close-and-invoice', methods=['POST'])
@login_required
@owner_required
def close_and_invoice(job_id):
    job = Job.query.filter_by(id=job_id, company_id=current_user.company_id).first_or_404()
    incomplete = [item for item in _json_list(job.closeout_checklist) if not item.get('done')]
    if incomplete:
        return jsonify({'error': 'Complete the closeout checklist before invoicing.'}), 400
    if job.signature_required and not job.signed_at:
        return jsonify({'error': 'Collect the customer signature before invoicing.'}), 400
    job.status = 'complete'
    if not job.completed_at:
        job.completed_at = datetime.utcnow()
    db.session.commit()
    _record_marketing_event(
        'first_job_completed',
        company_id=current_user.company_id,
        once=True,
    )
    invoice_result = _send_auto_invoice(job) or {}
    completion_message = 'The work is complete.'
    if invoice_result.get('invoice_sent'):
        completion_message += ' Your invoice and secure payment link have been emailed.'
    elif invoice_result.get('invoice_created'):
        completion_message += ' Your invoice has been prepared and will be sent separately.'
    _notify_client(job, 'completed', 'Service Complete', completion_message)
    if invoice_result.get('invoice_sent'):
        _record_marketing_event(
            'first_invoice_sent',
            company_id=current_user.company_id,
            once=True,
        )
    return jsonify({
        'success': True,
        **invoice_result,
    })

@app.route('/api/jobs/<int:job_id>/employee-notes', methods=['POST'])
@login_required
def save_employee_notes(job_id):
    job = _employee_job(job_id)
    job.employee_notes = request.json.get('notes', '').strip()
    db.session.commit()
    return jsonify({'success': True})

# ─────────────────────────────────────────
# EMAIL HELPER
# ─────────────────────────────────────────

def send_email(to_addr, subject, html_body):
    mail_user = os.environ.get('MAIL_USER')
    mail_pass = os.environ.get('MAIL_PASS')
    mail_from = os.environ.get('MAIL_FROM', mail_user)
    mail_host = os.environ.get('MAIL_HOST', 'smtp.gmail.com')
    mail_port = int(os.environ.get('MAIL_PORT', 587))
    if not mail_user or not mail_pass:
        app.logger.warning(f'Email not configured — would have sent "{subject}" to {to_addr}')
        return False
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = mail_from
        msg['To']      = to_addr
        msg.attach(MIMEText(html_body, 'html'))
        with smtplib.SMTP(mail_host, mail_port) as s:
            s.starttls()
            s.login(mail_user, mail_pass)
            s.sendmail(mail_from, to_addr, msg.as_string())
        return True
    except Exception as e:
        app.logger.error(f'Email send failed: {e}')
        return False


def _run_invoice_reminders(now):
    sent = 0
    skipped = 0
    candidates = InvoiceRecord.query.join(
        Company, Company.id == InvoiceRecord.company_id
    ).filter(
        Company.invoice_reminders_enabled == True,
        InvoiceRecord.status.in_(('sent', 'overdue', 'partial')),
        InvoiceRecord.due_date.isnot(None),
        InvoiceRecord.due_date < now,
    ).all()
    for invoice in candidates:
        if invoice.reminder_sent_at and invoice.reminder_sent_at > now - timedelta(days=7):
            skipped += 1
            continue
        invoice.status = 'overdue' if invoice.status == 'sent' else invoice.status
        try:
            if _send_invoice_record(invoice, reminder=True):
                sent += 1
            else:
                skipped += 1
        except Exception:
            db.session.rollback()
            app.logger.exception('Automatic invoice reminder failed for invoice %s', invoice.id)
            skipped += 1
    return {'sent': sent, 'skipped': skipped}


def _run_lifecycle_emails():
    """Send at most one behavior-based trial email per eligible company."""
    now = datetime.utcnow()
    sent = 0
    skipped = 0
    reminder_result = _run_invoice_reminders(now)
    companies = Company.query.filter_by(subscription_status='trialing').all()
    for company in companies:
        owner = User.query.filter_by(
            company_id=company.id,
            role='owner',
            is_active=True,
        ).first()
        if not owner or not owner.email:
            skipped += 1
            continue

        jobs = Job.query.filter_by(company_id=company.id).all()
        age_days = max(0, (now - company.created_at).days) if company.created_at else 0
        days_left = (
            max(0, (company.trial_ends_at - now).days)
            if company.trial_ends_at else None
        )
        email_kind = None
        subject = None
        body = None

        if days_left is not None and days_left <= 3:
            email_kind = 'trial_ending'
            subject = f'{days_left} day{"s" if days_left != 1 else ""} left in your FieldBase trial'
            body = (
                '<p>Your FieldBase trial is nearing its end.</p>'
                '<p>Open Settings to choose monthly or annual billing and keep your crew workflow active.</p>'
                '<p><a href="https://getfieldbase.net/settings">Review FieldBase plans</a></p>'
            )
        elif not jobs and age_days >= 1:
            email_kind = 'first_job'
            subject = 'Schedule your first real job in FieldBase'
            body = (
                '<p>Your FieldBase workspace is ready, but it does not have a job yet.</p>'
                '<p>Add one real upcoming job to see scheduling, technician assignment, and conflict checks in the same workflow.</p>'
                '<p><a href="https://getfieldbase.net/calendar">Add your first job</a></p>'
            )
        elif jobs and not any(job.invoice_sent for job in jobs) and age_days >= 4:
            email_kind = 'first_invoice'
            subject = 'Move your first FieldBase job into invoicing'
            body = (
                '<p>You have work in FieldBase, but no invoice has been recorded yet.</p>'
                '<p>Complete and review a real job, then use the owner dashboard to send its invoice or track payment.</p>'
                '<p><a href="https://getfieldbase.net/">Review your jobs</a></p>'
            )

        if not email_kind:
            skipped += 1
            continue

        event_name = f'lifecycle_email_{email_kind}'
        if MarketingEvent.query.filter_by(
            company_id=company.id,
            event_name=event_name,
        ).first():
            skipped += 1
            continue

        if send_email(owner.email, subject, body):
            _record_marketing_event(
                event_name,
                company_id=company.id,
                once=True,
            )
            sent += 1
        else:
            skipped += 1

    return {
        'sent': sent,
        'skipped': skipped,
        'invoice_reminders_sent': reminder_result['sent'],
        'invoice_reminders_skipped': reminder_result['skipped'],
    }


@app.route('/internal/lifecycle/run', methods=['POST'])
def run_lifecycle_emails():
    configured_secret = os.environ.get('FIELD_BASE_CRON_SECRET', '')
    if not configured_secret:
        abort(404)
    supplied = request.headers.get('Authorization', '')
    expected = f'Bearer {configured_secret}'
    if not hmac.compare_digest(supplied, expected):
        return jsonify({'error': 'unauthorized'}), 403
    return jsonify(_run_lifecycle_emails())


# ─────────────────────────────────────────
# PHOTO ROUTES
# ─────────────────────────────────────────

ALLOWED_EXT = {'jpg', 'jpeg', 'png', 'gif', 'webp'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT

@app.route('/api/jobs/<int:job_id>/photos', methods=['POST'])
@login_required
def upload_photo(job_id):
    job = Job.query.filter_by(id=job_id, company_id=current_user.company_id).first_or_404()
    if 'photo' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['photo']
    if not f or not allowed_file(f.filename):
        return jsonify({'error': 'Invalid file type'}), 400
    ext      = f.filename.rsplit('.', 1)[1].lower()
    filename = f'{uuid.uuid4().hex}.{ext}'
    storage.upload(f, 'photos', filename)
    photo = JobPhoto(job_id=job_id, company_id=current_user.company_id,
                     filename=filename, uploaded_by=current_user.name)
    db.session.add(photo)
    db.session.commit()
    return jsonify({'success': True, 'filename': filename, 'id': photo.id})


@app.route('/api/jobs/<int:job_id>/photos', methods=['GET'])
@login_required
def get_photos(job_id):
    Job.query.filter_by(id=job_id, company_id=current_user.company_id).first_or_404()
    photos = JobPhoto.query.filter_by(job_id=job_id).order_by(JobPhoto.uploaded_at).all()
    return jsonify([{
        'id':          p.id,
        'url':         storage.url('photos', p.filename),
        'uploaded_by': p.uploaded_by,
        'uploaded_at': p.uploaded_at.strftime('%b %d %I:%M %p')
    } for p in photos])


@app.route('/api/photos/<int:photo_id>', methods=['DELETE'])
@login_required
@owner_required
def delete_photo(photo_id):
    photo = JobPhoto.query.filter_by(id=photo_id, company_id=current_user.company_id).first_or_404()
    storage.delete('photos', photo.filename)
    db.session.delete(photo)
    db.session.commit()
    return jsonify({'success': True})


# ─────────────────────────────────────────
# DOCUMENT ROUTES
# ─────────────────────────────────────────

ALLOWED_DOC_EXT = {'pdf', 'doc', 'docx', 'xls', 'xlsx', 'png', 'jpg', 'jpeg', 'txt', 'csv'}

def allowed_doc(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_DOC_EXT


@app.route('/api/jobs/<int:job_id>/documents', methods=['POST'])
@login_required
@owner_required
def upload_document(job_id):
    Job.query.filter_by(id=job_id, company_id=current_user.company_id).first_or_404()
    if 'document' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['document']
    if not f or not allowed_doc(f.filename):
        return jsonify({'error': 'Invalid file type'}), 400
    original_name = f.filename
    ext      = f.filename.rsplit('.', 1)[1].lower()
    filename = f'{uuid.uuid4().hex}.{ext}'
    storage.upload(f, 'docs', filename)
    doc = JobDocument(job_id=job_id, company_id=current_user.company_id,
                      filename=filename, original_name=original_name,
                      uploaded_by=current_user.name)
    db.session.add(doc)
    db.session.commit()
    return jsonify({'success': True, 'id': doc.id, 'name': original_name,
                    'url': storage.url('docs', filename)})


@app.route('/api/jobs/<int:job_id>/documents', methods=['GET'])
@login_required
def get_documents(job_id):
    Job.query.filter_by(id=job_id, company_id=current_user.company_id).first_or_404()
    docs = JobDocument.query.filter_by(job_id=job_id).order_by(JobDocument.uploaded_at).all()
    return jsonify([{
        'id':          d.id,
        'name':        d.original_name or d.filename,
        'url':         storage.url('docs', d.filename),
        'uploaded_by': d.uploaded_by,
        'uploaded_at': d.uploaded_at.strftime('%b %d, %Y')
    } for d in docs])


@app.route('/api/documents/<int:doc_id>', methods=['DELETE'])
@login_required
@owner_required
def delete_document(doc_id):
    doc = JobDocument.query.filter_by(id=doc_id, company_id=current_user.company_id).first_or_404()
    storage.delete('docs', doc.filename)
    db.session.delete(doc)
    db.session.commit()
    return jsonify({'success': True})


# ─────────────────────────────────────────
# INVOICE EMAIL ROUTE
# ─────────────────────────────────────────

@app.route('/api/jobs/<int:job_id>/email-invoice', methods=['POST'])
@login_required
@owner_required
def email_invoice(job_id):
    job = Job.query.filter_by(id=job_id, company_id=current_user.company_id).first_or_404()
    data = request.json or {}
    job.client_email = _bounded(data.get('client_email'), 200) or job.client_email
    job.client_name = _bounded(data.get('client_name'), 200) or job.client_name
    if not job.client_email:
        return jsonify({'error': 'No client email provided.'}), 400
    if not job.job_pay or job.job_pay <= 0:
        return jsonify({'error': 'Add a positive job value before invoicing.'}), 400
    invoice = _create_invoice_for_job(job)
    invoice.client_email = job.client_email
    invoice.client_name = job.client_name
    db.session.commit()
    try:
        sent = _send_invoice_record(invoice)
    except Exception:
        app.logger.exception('Job invoice delivery failed')
        return jsonify({'error': 'Could not create the secure payment page.'}), 502
    if not sent:
        return jsonify({'error': 'Email is not configured or delivery failed.'}), 503
    _record_marketing_event('first_invoice_sent', company_id=current_user.company_id, once=True)
    return jsonify({
        'success': True,
        'sent_to': job.client_email,
        'invoice_id': invoice.id,
        'checkout_url': invoice.stripe_checkout_url,
    })


# ─────────────────────────────────────────
# REPORTS ROUTE
# ─────────────────────────────────────────

@app.route('/reports')
@login_required
@owner_required
def reports():
    from sqlalchemy import func, extract
    jobs = Job.query.filter_by(company_id=current_user.company_id).all()

    total_revenue   = sum(j.amount_paid or 0 for j in jobs if j.payment_received)
    total_jobs      = len(jobs)
    completed_jobs  = [j for j in jobs if j.status == 'complete']
    outstanding     = [j for j in jobs if j.status == 'complete' and not j.payment_received]
    outstanding_amt = sum(j.job_pay or 0 for j in outstanding)
    avg_value       = (sum(j.job_pay or 0 for j in jobs if j.job_pay) / max(1, len([j for j in jobs if j.job_pay])))

    # Platform breakdown
    platform_counts = {}
    for j in jobs:
        platform_counts[j.platform] = platform_counts.get(j.platform, 0) + 1

    # Monthly revenue (last 6 months)
    from datetime import timedelta
    monthly = {}
    for j in jobs:
        if j.payment_received and j.amount_paid:
            key = j.start_time.strftime('%b %Y')
            monthly[key] = monthly.get(key, 0) + j.amount_paid

    # Employee job counts
    emp_counts = {}
    for j in completed_jobs:
        if j.tech_assigned:
            emp_counts[j.tech_assigned] = emp_counts.get(j.tech_assigned, 0) + 1

    return render_template('reports.html',
        company=current_user.company,
        total_revenue=total_revenue,
        total_jobs=total_jobs,
        completed_jobs=len(completed_jobs),
        outstanding_amt=outstanding_amt,
        outstanding_count=len(outstanding),
        avg_value=avg_value,
        platform_counts=platform_counts,
        monthly=monthly,
        emp_counts=emp_counts,
    )


# ─────────────────────────────────────────
# WORK LOG
# ─────────────────────────────────────────

@app.route('/work-log')
@login_required
def work_log():
    if current_user.role == 'owner':
        company = Company.query.get(current_user.company_id)
        if company and not _has_active_access(company):
            return redirect(url_for('billing'))
        employees = User.query.filter_by(company_id=current_user.company_id, role='employee').order_by(User.name).all()
        completed = Job.query.filter_by(company_id=current_user.company_id, status='complete').order_by(Job.completed_at.desc()).all()
        return render_template('work_log.html', employees=employees, jobs=completed, viewer='owner')
    else:
        completed = Job.query.filter_by(company_id=current_user.company_id, tech_assigned=current_user.name, status='complete').order_by(Job.completed_at.desc()).all()
        return render_template('work_log.html', employees=[], jobs=completed, viewer='employee')

# ─────────────────────────────────────────
# SETTINGS ROUTES
# ─────────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
@login_required
@owner_required
def settings():
    company = db.session.get(Company, current_user.company_id)
    creds = {
        c.platform: c
        for c in PlatformCredential.query.filter_by(company_id=current_user.company_id).all()
    }
    if request.method == 'POST':
        company.invoice_reminders_enabled = request.form.get('invoice_reminders_enabled') == 'on'
        company.client_notifications_enabled = request.form.get('client_notifications_enabled') == 'on'
        for platform in ('workmarket', 'fieldnation'):
            api_key    = request.form.get(f'{platform}_key', '').strip()
            api_secret = request.form.get(f'{platform}_secret', '').strip()
            enabled    = request.form.get(f'{platform}_enabled') == 'on'
            if platform in creds:
                if api_key:
                    creds[platform].api_key = api_key
                if api_secret:
                    creds[platform].api_secret = api_secret
                creds[platform].enabled    = enabled
                creds[platform].updated_at = datetime.utcnow()
            else:
                db.session.add(PlatformCredential(
                    company_id=current_user.company_id,
                    platform=platform,
                    api_key=api_key,
                    api_secret=api_secret,
                    enabled=enabled
                ))
        db.session.commit()
        flash('Settings saved.')
        return redirect(url_for('settings'))
    return render_template('settings.html', creds=creds, company=company)


@app.route('/growth')
@login_required
def growth_dashboard():
    admin_emails = {
        email.strip().lower()
        for email in os.environ.get('FIELD_BASE_ADMIN_EMAILS', '').split(',')
        if email.strip()
    }
    if current_user.email.lower() not in admin_emails:
        abort(404)

    internal_emails = {
        email.strip().lower()
        for email in os.environ.get('FIELD_BASE_INTERNAL_EMAILS', '').split(',')
        if email.strip()
    } | admin_emails
    internal_company_ids = {
        user.company_id
        for user in User.query.filter(User.email.in_(internal_emails)).all()
    } if internal_emails else set()

    cutoff = datetime.utcnow() - timedelta(days=30)
    events = MarketingEvent.query.order_by(MarketingEvent.created_at.desc()).all()
    events = [
        event for event in events
        if not event.company_id or event.company_id not in internal_company_ids
    ]

    stages = [
        ('landing_view', 'Qualified visits', 'visitor'),
        ('registration_started', 'Registration starts', 'visitor'),
        ('registration_completed', 'Trials created', 'company'),
        ('first_job_created', 'First jobs created', 'company'),
        ('first_job_completed', 'First jobs completed', 'company'),
        ('first_invoice_sent', 'First invoices sent', 'company'),
        ('checkout_started', 'Checkout starts', 'company'),
        ('subscription_activated', 'Paid companies', 'company'),
    ]

    def stage_count(name, entity, recent=False):
        matching = [
            event for event in events
            if event.event_name == name
            and (not recent or event.created_at >= cutoff)
        ]
        if entity == 'company':
            return len({event.company_id for event in matching if event.company_id})
        return len({event.visitor_id for event in matching if event.visitor_id})

    funnel = []
    previous_all = None
    previous_30d = None
    for event_name, label, entity in stages:
        count_all = stage_count(event_name, entity)
        count_30d = stage_count(event_name, entity, recent=True)
        funnel.append({
            'event_name': event_name,
            'label': label,
            'all_time': count_all,
            'last_30_days': count_30d,
            'step_rate_all': round(count_all / previous_all * 100, 1) if previous_all else None,
            'step_rate_30d': round(count_30d / previous_30d * 100, 1) if previous_30d else None,
        })
        previous_all = count_all
        previous_30d = count_30d

    source_counts = {}
    for event in events:
        if event.event_name != 'registration_completed' or not event.company_id:
            continue
        source = event.source or 'unknown'
        source_counts[source] = source_counts.get(source, 0) + 1

    return render_template(
        'growth.html',
        funnel=funnel,
        source_counts=sorted(source_counts.items(), key=lambda item: item[1], reverse=True),
        generated_at=datetime.utcnow(),
    )


# ─────────────────────────────────────────
# TEAM MEMBERS API
# ─────────────────────────────────────────

@app.route('/api/team/members')
@login_required
@owner_required
def team_members():
    employees = User.query.filter(
        User.company_id == current_user.company_id,
        User.is_active == True,
        User.role.in_(['owner', 'employee'])
    ).order_by(User.name).all()
    return jsonify([{'name': e.name} for e in employees])


# ─────────────────────────────────────────
# IMAGE-TO-JOB (screenshot upload)
# ─────────────────────────────────────────

@app.route('/api/image-to-job', methods=['POST'])
@login_required
@owner_required
def image_to_job():
    import anthropic as _anthropic
    import base64, json, re
    file = request.files.get('image')
    if not file:
        return jsonify({'error': 'No image provided'}), 400
    image_data = base64.standard_b64encode(file.read()).decode('utf-8')
    media_type = file.content_type or 'image/jpeg'
    if media_type not in ('image/jpeg', 'image/png', 'image/gif', 'image/webp'):
        media_type = 'image/jpeg'
    today = datetime.utcnow().strftime('%Y-%m-%d')
    try:
        ai = _anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        msg = ai.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1024,
            messages=[{'role': 'user', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': image_data}},
                {'type': 'text', 'text': (
                    f'Extract job details from this screenshot. Today is {today}. '
                    'Return ONLY valid JSON (no extra text): '
                    '{"title":"...","platform":"workmarket|fieldnation|direct|email|phone|manual",'
                    '"location":"full address","client_name":"...","client_company":"...",'
                    '"client_email":"...","assigned_employee":"first name",'
                    '"start":"YYYY-MM-DDTHH:MM","end":"YYYY-MM-DDTHH:MM","notes":"..."} '
                    'Use null for missing fields.'
                )}
            ]}]
        )
        text = msg.content[0].text.strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            return jsonify({'error': 'Could not read image content'}), 500
        return jsonify(json.loads(m.group()))
    except Exception as e:
        app.logger.exception('image_to_job failed')
        return jsonify({'error': 'Could not extract job details from that image.'}), 500


# ─────────────────────────────────────────
# VOICE-TO-JOB
# ─────────────────────────────────────────

@app.route('/api/voice/transcription', methods=['GET', 'POST'])
@login_required
@owner_required
def voice_transcription():
    if request.method == 'GET':
        return jsonify({'configured': transcription_is_configured()})

    if not transcription_is_configured():
        return jsonify({
            'error': 'Server transcription is not configured.',
            'fallback_available': True,
        }), 503

    audio = request.files.get('audio')
    if not audio:
        return jsonify({'error': 'No audio recording provided.'}), 400

    if request.content_length and request.content_length > MAX_AUDIO_BYTES + 64 * 1024:
        return jsonify({'error': 'Audio recording exceeds the 15 MB upload limit.'}), 413

    audio_bytes = audio.stream.read(MAX_AUDIO_BYTES + 1)
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        return jsonify({'error': 'Audio recording exceeds the 15 MB upload limit.'}), 413

    try:
        transcript = transcribe_audio(
            audio_bytes,
            filename=audio.filename,
            content_type=audio.content_type,
        )
        return jsonify({'transcript': transcript})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except TranscriptionConfigurationError:
        return jsonify({
            'error': 'Server transcription is not configured.',
            'fallback_available': True,
        }), 503
    except TranscriptionProviderError as e:
        app.logger.warning(
            'Voice transcription provider failure (status=%s): %s',
            e.status_code,
            e,
        )
        return jsonify({'error': str(e), 'fallback_available': True}), 502

@app.route('/api/voice-to-job', methods=['POST'])
@login_required
@owner_required
def voice_to_job():
    import anthropic as ant
    import json as json_lib
    from datetime import date

    data = request.json or {}
    transcript = (data.get('transcript') or '').strip()
    if not transcript:
        return jsonify({'error': 'No transcript provided'}), 400

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'error': 'ANTHROPIC_API_KEY not configured in Railway'}), 500

    today = date.today().strftime('%A, %B %d, %Y')

    prompt = f"""Today is {today}.

Extract job details from this voice transcript. Return ONLY a valid JSON object with these exact fields:
- "title": short job title (required, never empty)
- "platform": one of: workmarket, fieldnation, direct, email, phone, manual (default "phone" for phone calls)
- "location": full job site address (empty string if not mentioned)
- "client_name": contact person's name — just the person, not their company (empty string if not mentioned)
- "client_company": the company or organization the client works for or represents (empty string if not mentioned)
- "client_email": client's email address (empty string if not mentioned)
- "assigned_employee": full name of the employee/tech to assign this job to — extracted from phrases like "assign to Glenn", "this is for Glenn Dinkins", "send Glenn" (empty string if not mentioned)
- "tech_pay": number — what the employee gets paid, from phrases like "pays 150", "tech gets 200" (empty string if not mentioned)
- "job_pay": number — total job value / what the client is billed, from phrases like "billing 400", "job is worth 500" (empty string if not mentioned; if only one dollar amount is mentioned without context, put it in tech_pay)
- "start": start datetime as YYYY-MM-DDTHH:MM (empty string if not mentioned)
- "end": end datetime as YYYY-MM-DDTHH:MM (empty string if not mentioned)
- "notes": scope of work, tools required, and any other relevant details (empty string if none)

Rules:
- Convert relative dates to absolute dates based on today. "Tomorrow" = next calendar day, "next Monday" = the coming Monday, etc.
- If only a date with no time is mentioned, use 08:00 for start and 17:00 for end as defaults.
- If a duration is mentioned ("3 hours"), calculate end time from start.
- If no date or time is mentioned at all, leave start and end as empty strings.
- If tools are mentioned, include them in notes as "Tools needed: ...".
- Return only the raw JSON object — no markdown, no code fences, no explanation.

Transcript: "{transcript}"
"""

    client = ant.Anthropic(api_key=api_key)
    message = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=500,
        messages=[{'role': 'user', 'content': prompt}]
    )

    try:
        text = message.content[0].text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        result = json_lib.loads(text)
        return jsonify(result)
    except Exception as e:
        app.logger.error(f'voice-to-job parse error: {e} | raw: {message.content[0].text}')
        return jsonify({'error': 'Could not parse job details from transcript'}), 500


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def platform_color(platform):
    return {
        'workmarket':  '#2563eb',
        'fieldnation': '#16a34a',
        'manual':      '#9333ea'
    }.get(platform, '#6b7280')



# ─────────────────────────────────────────
# RECEIPT LOGGER
# ─────────────────────────────────────────

RECEIPT_CATEGORIES = ['Parts', 'Tools', 'Travel', 'Fuel', 'Food', 'Supplies', 'Subcontractor', 'Other']

@app.route('/receipts')
@login_required
@owner_required
def receipts():
    all_receipts = Receipt.query.filter_by(company_id=current_user.company_id).order_by(Receipt.uploaded_at.desc()).all()
    jobs = Job.query.filter_by(company_id=current_user.company_id).order_by(Job.start_time.desc()).all()
    total = sum(r.amount or 0 for r in all_receipts)
    by_category = {}
    for r in all_receipts:
        cat = r.category or 'Uncategorized'
        by_category[cat] = by_category.get(cat, 0) + (r.amount or 0)
    return render_template('receipts.html', receipts=all_receipts, jobs=jobs,
                           total=total, by_category=by_category, categories=RECEIPT_CATEGORIES)

@app.route('/api/receipts', methods=['POST'])
@login_required
@owner_required
def upload_receipt():
    file = request.files.get('file')
    if not file or file.filename == '':
        return jsonify({'error': 'No file'}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ['.jpg', '.jpeg', '.png', '.gif', '.pdf', '.heic']:
        return jsonify({'error': 'Invalid file type'}), 400
    safe_name = f"{uuid.uuid4().hex}{ext}"
    storage.upload(file, 'receipts', safe_name)
    r = Receipt(
        company_id  = current_user.company_id,
        job_id      = request.form.get('job_id') or None,
        filename    = safe_name,
        category    = request.form.get('category', 'Other'),
        amount      = _to_float(request.form.get('amount')) or 0,
        vendor      = request.form.get('vendor', ''),
        description = request.form.get('description', ''),
        uploaded_by = current_user.name,
    )
    db.session.add(r)
    db.session.commit()
    return jsonify({'id': r.id, 'filename': r.filename, 'category': r.category}), 201

@app.route('/api/receipts/<int:receipt_id>', methods=['DELETE'])
@login_required
@owner_required
def delete_receipt(receipt_id):
    r = Receipt.query.filter_by(id=receipt_id, company_id=current_user.company_id).first_or_404()
    storage.delete('receipts', r.filename)
    db.session.delete(r)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/uploads/receipts/<path:filename>')
@login_required
def serve_receipt(filename):
    Receipt.query.filter_by(filename=filename, company_id=current_user.company_id).first_or_404()
    if storage.USE_R2:
        return redirect(storage.url('receipts', filename))
    return send_from_directory(os.path.join(_ROOT, 'uploads', 'receipts'), filename)


# ─────────────────────────────────────────
# TECH STANDARDS CARD
# ─────────────────────────────────────────

@app.route('/tech-standards', methods=['GET', 'POST'])
@login_required
@owner_required
def tech_standards():
    std = TechStandard.query.filter_by(company_id=current_user.company_id).first()
    if not std:
        std = TechStandard(company_id=current_user.company_id)
        db.session.add(std)
        db.session.commit()
    if request.method == 'POST':
        std.dress_code   = request.form.get('dress_code', std.dress_code)
        std.eta_rules    = request.form.get('eta_rules', std.eta_rules)
        std.deliverables = request.form.get('deliverables', std.deliverables)
        std.safety_rules = request.form.get('safety_rules', std.safety_rules)
        std.updated_at   = datetime.utcnow()
        db.session.commit()
        flash('Tech Standards updated.', 'success')
        return redirect(url_for('tech_standards'))
    return render_template('tech_standards.html', std=std)

@app.route('/api/tech-standards/pdf')
@login_required
@owner_required
def tech_standards_pdf():
    std = TechStandard.query.filter_by(company_id=current_user.company_id).first()
    if not std:
        return jsonify({'error': 'No standards set'}), 404
    company = current_user.company
    html = f"""
    <html><body style="font-family:Arial,sans-serif;padding:40px;max-width:700px;margin:auto;">
    <h1 style="color:#1e3a5f;border-bottom:2px solid #1e3a5f;padding-bottom:10px;">{company.name}</h1>
    <h2 style="color:#1e3a5f;">Tech Standards Card</h2>
    <h3>Dress Code</h3><p>{std.dress_code}</p>
    <h3>ETA Rules</h3><p>{std.eta_rules}</p>
    <h3>Deliverables</h3><p>{std.deliverables}</p>
    <h3>Safety Rules</h3><p>{std.safety_rules}</p>
    <p style="color:#9ca3af;font-size:12px;margin-top:40px;">Last updated: {std.updated_at.strftime('%B %d, %Y')}</p>
    </body></html>
    """
    from flask import Response
    return Response(html, mimetype='text/html',
                    headers={'Content-Disposition': 'attachment; filename=tech_standards.html'})


# ─────────────────────────────────────────
# PLATFORM SYNC — WorkMarket + Field Nation
# ─────────────────────────────────────────

@app.route('/api/sync-platform', methods=['POST'])
@login_required
@owner_required
def sync_platform():
    """Pull jobs from WorkMarket and Field Nation using stored API credentials."""
    import requests as req_lib
    company_id = current_user.company_id
    creds = {c.platform: c for c in PlatformCredential.query.filter_by(company_id=company_id).all()}
    results = {'workmarket': 0, 'fieldnation': 0, 'errors': []}

    # ── WorkMarket ──────────────────────────────────────────────────
    wm = creds.get('workmarket')
    if wm and wm.api_key and wm.enabled:
        try:
            headers = {'Authorization': f'Bearer {wm.api_key}', 'Accept': 'application/json'}
            resp = req_lib.get(
                'https://api.workmarket.com/v1/assignments?status=active&per_page=50',
                headers=headers, timeout=10
            )
            if resp.status_code == 200:
                assignments = resp.json().get('results', resp.json().get('assignments', []))
                for a in assignments:
                    ext_id = f"wm_{a.get('id', '')}"
                    existing = Job.query.filter_by(company_id=company_id, external_job_id=ext_id).first()
                    if existing:
                        continue
                    # Parse times — WorkMarket uses schedule.from / schedule.through
                    sched = a.get('schedule', {})
                    start_str = sched.get('from') or a.get('start_time') or a.get('created_at')
                    end_str   = sched.get('through') or a.get('end_time') or start_str
                    if not start_str:
                        continue
                    try:
                        from dateutil import parser as dateparser
                        start_dt = dateparser.parse(start_str)
                        end_dt   = dateparser.parse(end_str) if end_str != start_str else start_dt.replace(hour=start_dt.hour+1)
                    except Exception as e:
                        app.logger.warning(
                            'Skipped WorkMarket assignment %s with invalid schedule: %s',
                            a.get('id', ''),
                            e,
                        )
                        continue
                    location = a.get('location', {})
                    addr = location.get('full_address') or location.get('address1') or '' if isinstance(location, dict) else str(location)
                    job = Job(
                        company_id=company_id,
                        title=a.get('title') or a.get('name') or 'WorkMarket Job',
                        platform='workmarket',
                        location=addr,
                        start_time=start_dt,
                        end_time=end_dt,
                        status='scheduled',
                        external_job_id=ext_id,
                        notes=f"Synced from WorkMarket. ID: {a.get('id', '')}",
                    )
                    db.session.add(job)
                    results['workmarket'] += 1
                db.session.commit()
            else:
                results['errors'].append(f'WorkMarket: HTTP {resp.status_code}')
        except Exception as e:
            results['errors'].append(f'WorkMarket: {str(e)}')

    # ── Field Nation ─────────────────────────────────────────────────
    fn = creds.get('fieldnation')
    if fn and fn.api_key and fn.enabled:
        try:
            headers = {'Authorization': f'Bearer {fn.api_key}', 'Accept': 'application/json'}
            resp = req_lib.get(
                'https://app.fieldnation.com/api/rest/v2/workorders?status_id=1,2&per_page=50',
                headers=headers, timeout=10
            )
            if resp.status_code == 200:
                workorders = resp.json().get('results', {}).get('workorders', resp.json().get('results', []))
                if isinstance(workorders, dict):
                    workorders = workorders.get('workorders', [])
                for wo in workorders:
                    ext_id = f"fn_{wo.get('id', '')}"
                    existing = Job.query.filter_by(company_id=company_id, external_job_id=ext_id).first()
                    if existing:
                        continue
                    sched = wo.get('schedule', {}) or {}
                    start_str = sched.get('start') or wo.get('start_time')
                    end_str   = sched.get('end')   or wo.get('end_time') or start_str
                    if not start_str:
                        continue
                    try:
                        from dateutil import parser as dateparser
                        start_dt = dateparser.parse(start_str)
                        end_dt   = dateparser.parse(end_str) if end_str and end_str != start_str else start_dt.replace(hour=min(start_dt.hour+1,23))
                    except Exception as e:
                        app.logger.warning(
                            'Skipped Field Nation work order %s with invalid schedule: %s',
                            wo.get('id', ''),
                            e,
                        )
                        continue
                    location = wo.get('location', {}) or {}
                    addr = location.get('address1') or location.get('city') or '' if isinstance(location, dict) else ''
                    job = Job(
                        company_id=company_id,
                        title=wo.get('title') or 'Field Nation Job',
                        platform='fieldnation',
                        location=addr,
                        start_time=start_dt,
                        end_time=end_dt,
                        status='scheduled',
                        external_job_id=ext_id,
                        notes=f"Synced from Field Nation. ID: {wo.get('id', '')}",
                    )
                    db.session.add(job)
                    results['fieldnation'] += 1
                db.session.commit()
            else:
                results['errors'].append(f'Field Nation: HTTP {resp.status_code}')
        except Exception as e:
            results['errors'].append(f'Field Nation: {str(e)}')

    detect_and_save_conflicts(company_id)
    return jsonify({
        'success': True,
        'synced': results,
        'message': f"Synced {results['workmarket']} WorkMarket + {results['fieldnation']} Field Nation jobs."
    })

def detect_conflicts(company_id):
    # Only upcoming, unfinished jobs can meaningfully conflict — completed and
    # long-past jobs would otherwise pile up as permanent false alarms (and
    # make this O(n²) scan slower every month).
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=1)
    jobs = (Job.query.filter_by(company_id=company_id)
            .filter(Job.status != 'complete', Job.end_time >= cutoff)
            .order_by(Job.start_time).all())
    conflicts = []
    for i in range(len(jobs)):
        for j in range(i + 1, len(jobs)):
            a, b = jobs[i], jobs[j]
            # Only flag conflict if same tech is double booked
            same_tech = (
                a.tech_assigned and b.tech_assigned and
                a.tech_assigned.strip().lower() == b.tech_assigned.strip().lower()
            )
            if same_tech and a.start_time < b.end_time and b.start_time < a.end_time:
                conflicts.append({
                    'job_a': a.title, 'job_b': b.title,
                    'job_a_id': a.id, 'job_b_id': b.id,
                    'start_a': a.start_time.isoformat(),
                    'start_b': b.start_time.isoformat(),
                    'tech': a.tech_assigned
                })
    return conflicts


def detect_and_save_conflicts(company_id):
    Conflict.query.filter_by(company_id=company_id, resolved=False).delete()
    for c in detect_conflicts(company_id):
        db.session.add(Conflict(
            company_id=company_id,
            job_a_id=c['job_a_id'],
            job_b_id=c['job_b_id']
        ))
    db.session.commit()

# ─────────────────────────────────────────
# BILLING
# ─────────────────────────────────────────

@app.route('/billing')
@login_required
def billing():
    # Billing now lives under Settings; keep this route as a redirect so old
    # links, bookmarks, and the trial banner still resolve.
    if current_user.role != 'owner':
        return redirect(url_for('employee_dashboard'))
    return redirect(url_for('settings'))


@app.route('/billing/create-checkout', methods=['POST'])
@login_required
def create_checkout():
    if current_user.role != 'owner':
        return jsonify({'error': 'unauthorized'}), 403
    company = Company.query.get(current_user.company_id)
    requested_plan = (request.get_json(silent=True) or {}).get('plan', 'monthly')
    price_ids = {
        'monthly': STRIPE_PRICE_ID,
        'founding_annual': STRIPE_ANNUAL_PRICE_ID,
    }
    if requested_plan not in price_ids:
        return jsonify({'error': 'Unknown billing plan.'}), 400
    selected_price_id = price_ids[requested_plan]
    if not selected_price_id:
        return jsonify({'error': 'Billing not configured yet.'}), 500
    _record_marketing_event(
        'checkout_started',
        company_id=current_user.company_id,
        details={'plan': requested_plan},
    )
    if not company.stripe_customer_id:
        customer = stripe.Customer.create(
            email=current_user.email,
            name=company.name,
            metadata={'company_id': company.id}
        )
        company.stripe_customer_id = customer.id
        db.session.commit()
    session = stripe.checkout.Session.create(
        customer=company.stripe_customer_id,
        mode='subscription',
        line_items=[{'price': selected_price_id, 'quantity': 1}],
        allow_promotion_codes=True,
        success_url=request.host_url + 'billing/success',
        cancel_url=request.host_url + 'settings',
    )
    return jsonify({'url': session.url})


@app.route('/billing/success')
@login_required
def billing_success():
    flash('Subscription active — welcome to FieldBase!')
    return redirect(url_for('index'))


@app.route('/billing/portal', methods=['POST'])
@login_required
def billing_portal():
    if current_user.role != 'owner':
        return jsonify({'error': 'unauthorized'}), 403
    company = Company.query.get(current_user.company_id)
    if not company.stripe_customer_id:
        return jsonify({'error': 'No subscription found'}), 400
    session = stripe.billing_portal.Session.create(
        customer=company.stripe_customer_id,
        return_url=request.host_url + 'billing',
    )
    return jsonify({'url': session.url})


# ─────────────────────────────────────────
# STRIPE CONNECT — each owner links their own account so client job
# payments land in THEIR bank, not the platform's.
# ─────────────────────────────────────────

@app.route('/billing/connect', methods=['POST'])
@login_required
def connect_onboard():
    if current_user.role != 'owner':
        return jsonify({'error': 'unauthorized'}), 403
    company = Company.query.get(current_user.company_id)
    try:
        if not company.stripe_connect_id:
            acct = stripe.Account.create(
                type='express',
                email=current_user.email,
                business_profile={'name': company.name},
                metadata={'company_id': company.id},
            )
            company.stripe_connect_id = acct.id
            db.session.commit()
        link = stripe.AccountLink.create(
            account=company.stripe_connect_id,
            refresh_url=request.host_url + 'settings',
            return_url=request.host_url + 'settings?connected=1',
            type='account_onboarding',
        )
        return jsonify({'url': link.url})
    except Exception as e:
        app.logger.error(f'Connect onboarding failed for company {company.id}: {e}')
        return jsonify({'error': 'Could not start payout setup. Try again.'}), 500


@app.route('/billing/connect/dashboard', methods=['POST'])
@login_required
def connect_dashboard():
    if current_user.role != 'owner':
        return jsonify({'error': 'unauthorized'}), 403
    company = Company.query.get(current_user.company_id)
    if not company.stripe_connect_id:
        return jsonify({'error': 'No payout account connected yet.'}), 400
    try:
        link = stripe.Account.create_login_link(company.stripe_connect_id)
        return jsonify({'url': link.url})
    except Exception as e:
        app.logger.error(f'Connect dashboard link failed for company {company.id}: {e}')
        return jsonify({'error': 'Could not open payout dashboard.'}), 500


@app.route('/billing/connect/status')
@login_required
def connect_status():
    """Refresh charges_enabled from Stripe on return from onboarding (webhook is the
    source of truth, but this gives instant feedback without waiting for it)."""
    if current_user.role != 'owner':
        return jsonify({'error': 'unauthorized'}), 403
    company = Company.query.get(current_user.company_id)
    if not company.stripe_connect_id:
        return jsonify({'connected': False, 'charges_enabled': False})
    try:
        acct = stripe.Account.retrieve(company.stripe_connect_id)
        company.connect_charges_enabled = bool(acct.get('charges_enabled'))
        db.session.commit()
    except Exception as e:
        app.logger.error(f'Connect status check failed for company {company.id}: {e}')
    return jsonify({'connected': True, 'charges_enabled': company.connect_charges_enabled})


@app.route('/stripe/webhook', methods=['POST'])
def stripe_webhook():
    payload    = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    event = None
    for secret in (STRIPE_WEBHOOK_SECRET, STRIPE_CONNECT_WEBHOOK_SECRET):
        if not secret:
            continue
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, secret)
            break
        except Exception:
            app.logger.debug('Stripe webhook signature did not match one configured secret.')
            continue
    if event is None:
        return jsonify({'error': 'Invalid signature'}), 400
    obj = event['data']['object']
    if event['type'] in ('customer.subscription.created', 'customer.subscription.updated'):
        company = Company.query.filter_by(stripe_customer_id=obj['customer']).first()
        if company:
            company.stripe_subscription_id = obj['id']
            company.subscription_status    = obj['status']
            db.session.commit()
            if obj['status'] == 'active':
                _record_marketing_event(
                    'subscription_activated',
                    company_id=company.id,
                    once=True,
                )
    elif event['type'] == 'customer.subscription.deleted':
        company = Company.query.filter_by(stripe_customer_id=obj['customer']).first()
        if company:
            company.subscription_status = 'canceled'
            db.session.commit()
    elif event['type'] == 'checkout.session.completed':
        metadata = obj.get('metadata', {})
        invoice_id = metadata.get('invoice_id')
        job_id = metadata.get('job_id')
        amount_paid = (obj.get('amount_total') or 0) / 100
        invoice = db.session.get(InvoiceRecord, int(invoice_id)) if invoice_id else None
        if invoice and invoice.status != 'paid':
            invoice.status = 'paid'
            invoice.amount_paid = amount_paid
            invoice.paid_at = datetime.utcnow()
            invoice.stripe_checkout_session_id = obj.get('id')
            if invoice.job_id:
                job = db.session.get(Job, invoice.job_id)
                if job:
                    job.payment_received = True
                    job.amount_paid = amount_paid
            db.session.commit()
            _record_marketing_event(
                'first_payment_recorded',
                company_id=invoice.company_id,
                once=True,
            )
        elif job_id:
            job = db.session.get(Job, int(job_id))
            if job and not job.payment_received:
                job.payment_received = True
                job.amount_paid = amount_paid
                db.session.commit()
                _record_marketing_event(
                    'first_payment_recorded',
                    company_id=job.company_id,
                    once=True,
                )
    elif event['type'] == 'account.updated':
        # A connected (Express) account finished/updated onboarding.
        company = Company.query.filter_by(stripe_connect_id=obj['id']).first()
        if company:
            company.connect_charges_enabled = bool(obj.get('charges_enabled'))
            db.session.commit()
    return jsonify({'status': 'ok'})


# ─────────────────────────────────────────
# INIT — runs on every startup (Gunicorn + direct)
# ─────────────────────────────────────────

with app.app_context():
    db.create_all()
    with db.engine.connect() as conn:
        for col, ddl in [
            ('tech_confirmed',   'ALTER TABLE jobs ADD COLUMN tech_confirmed BOOLEAN DEFAULT FALSE'),
            ('confirmed_at',     'ALTER TABLE jobs ADD COLUMN confirmed_at TIMESTAMP'),
            ('clock_in_at',      'ALTER TABLE jobs ADD COLUMN clock_in_at TIMESTAMP'),
            ('clock_out_at',     'ALTER TABLE jobs ADD COLUMN clock_out_at TIMESTAMP'),
            ('completed_at',     'ALTER TABLE jobs ADD COLUMN completed_at TIMESTAMP'),
            ('employee_notes',   'ALTER TABLE jobs ADD COLUMN employee_notes TEXT'),
            ('invoice_sent',     'ALTER TABLE jobs ADD COLUMN invoice_sent BOOLEAN DEFAULT FALSE'),
            ('invoice_sent_at',  'ALTER TABLE jobs ADD COLUMN invoice_sent_at TIMESTAMP'),
            ('payment_received', 'ALTER TABLE jobs ADD COLUMN payment_received BOOLEAN DEFAULT FALSE'),
            ('amount_paid',      'ALTER TABLE jobs ADD COLUMN amount_paid FLOAT'),
            ('hourly_rate',      'ALTER TABLE users ADD COLUMN hourly_rate FLOAT'),
            ('client_name',      'ALTER TABLE jobs ADD COLUMN client_name VARCHAR(200)'),
            ('client_email',     'ALTER TABLE jobs ADD COLUMN client_email VARCHAR(200)'),
            ('external_job_id',  'ALTER TABLE jobs ADD COLUMN external_job_id VARCHAR(200)'),
            ('clock_in_lat',     'ALTER TABLE jobs ADD COLUMN clock_in_lat FLOAT'),
            ('clock_in_lng',     'ALTER TABLE jobs ADD COLUMN clock_in_lng FLOAT'),
            ('client_company',   'ALTER TABLE jobs ADD COLUMN client_company VARCHAR(200)'),
            ('job_lat',          'ALTER TABLE jobs ADD COLUMN job_lat FLOAT'),
            ('job_lng',          'ALTER TABLE jobs ADD COLUMN job_lng FLOAT'),
            ('client_id',        'ALTER TABLE jobs ADD COLUMN client_id INTEGER REFERENCES clients(id)'),
            ('job_template_id',  'ALTER TABLE jobs ADD COLUMN job_template_id INTEGER REFERENCES job_templates(id)'),
            ('closeout_checklist', 'ALTER TABLE jobs ADD COLUMN closeout_checklist TEXT DEFAULT \'[]\''),
            ('signature_name',   'ALTER TABLE jobs ADD COLUMN signature_name VARCHAR(200)'),
            ('signature_filename', 'ALTER TABLE jobs ADD COLUMN signature_filename VARCHAR(300)'),
            ('signed_at',        'ALTER TABLE jobs ADD COLUMN signed_at TIMESTAMP'),
            ('signature_required', 'ALTER TABLE jobs ADD COLUMN signature_required BOOLEAN DEFAULT FALSE'),
        ('receipt_cat',      'CREATE TABLE IF NOT EXISTS receipts (id SERIAL PRIMARY KEY, company_id INTEGER REFERENCES companies(id), job_id INTEGER REFERENCES jobs(id), filename VARCHAR(300) NOT NULL, category VARCHAR(100) DEFAULT \'Uncategorized\', amount FLOAT, vendor VARCHAR(200), description TEXT, uploaded_by VARCHAR(200), uploaded_at TIMESTAMP DEFAULT NOW())'),
        ('tech_std',         'CREATE TABLE IF NOT EXISTS tech_standards (id SERIAL PRIMARY KEY, company_id INTEGER UNIQUE REFERENCES companies(id), dress_code TEXT, eta_rules TEXT, deliverables TEXT, safety_rules TEXT, updated_at TIMESTAMP DEFAULT NOW())'),
        ('stripe_payment_link',     'ALTER TABLE jobs ADD COLUMN stripe_payment_link VARCHAR(500)'),
        ('stripe_customer_id',     'ALTER TABLE companies ADD COLUMN stripe_customer_id VARCHAR(100)'),
        ('stripe_subscription_id', 'ALTER TABLE companies ADD COLUMN stripe_subscription_id VARCHAR(100)'),
        ('subscription_status',    'ALTER TABLE companies ADD COLUMN subscription_status VARCHAR(20)'),
        ('trial_ends_at',          'ALTER TABLE companies ADD COLUMN trial_ends_at TIMESTAMP'),
        ('stripe_connect_id',       'ALTER TABLE companies ADD COLUMN stripe_connect_id VARCHAR(100)'),
        ('connect_charges_enabled', 'ALTER TABLE companies ADD COLUMN connect_charges_enabled BOOLEAN DEFAULT FALSE'),
        ('trade_type',              'ALTER TABLE companies ADD COLUMN trade_type VARCHAR(100)'),
        ('acquisition_source',      'ALTER TABLE companies ADD COLUMN acquisition_source VARCHAR(100)'),
        ('acquisition_medium',      'ALTER TABLE companies ADD COLUMN acquisition_medium VARCHAR(100)'),
        ('acquisition_campaign',    'ALTER TABLE companies ADD COLUMN acquisition_campaign VARCHAR(100)'),
        ('acquisition_content',     'ALTER TABLE companies ADD COLUMN acquisition_content VARCHAR(100)'),
        ('acquisition_landing',     'ALTER TABLE companies ADD COLUMN acquisition_landing VARCHAR(300)'),
        ('invoice_reminders_enabled', 'ALTER TABLE companies ADD COLUMN invoice_reminders_enabled BOOLEAN DEFAULT FALSE'),
        ('client_notifications_enabled', 'ALTER TABLE companies ADD COLUMN client_notifications_enabled BOOLEAN DEFAULT FALSE'),
        ]:
            try:
                conn.execute(text(ddl))
                conn.commit()
            except Exception:
                conn.rollback()

if __name__ == '__main__':
    app.run(debug=False, port=5050)
