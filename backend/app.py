from flask import Flask, render_template, jsonify, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from datetime import datetime
from functools import wraps
import bcrypt
import os

_BASE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_BASE)

app = Flask(__name__,
    template_folder=os.path.join(_ROOT, 'frontend', 'templates'),
    static_folder=os.path.join(_ROOT, 'frontend', 'static'))

app.secret_key = os.environ.get('SECRET_KEY', 'fieldbase_dev_secret')

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://dinx@localhost/fieldbase_saas')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
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
    is_active  = db.Column(db.Boolean, default=True)
    users      = db.relationship('User', backref='company', lazy=True)
    jobs       = db.relationship('Job', backref='company', lazy=True)


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
    tech_confirmed = db.Column(db.Boolean, default=False)
    invoice_sent   = db.Column(db.Boolean, default=False)
    invoice_sent_at= db.Column(db.DateTime)
    notes          = db.Column(db.Text)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)


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


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ─────────────────────────────────────────
# DECORATORS
# ─────────────────────────────────────────

def owner_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'owner':
            return redirect(url_for('employee_dashboard'))
        return f(*args, **kwargs)
    return decorated

# ─────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        company_name = request.form.get('company_name', '').strip()
        name         = request.form.get('name', '').strip()
        email        = request.form.get('email', '').strip().lower()
        password     = request.form.get('password', '')

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

        company = Company(name=company_name, slug=slug)
        db.session.add(company)
        db.session.flush()

        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        user = User(company_id=company.id, email=email, name=name,
                    password_hash=pw_hash, role='owner')
        db.session.add(user)
        db.session.commit()

        login_user(user)
        return redirect(url_for('index'))

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
@login_required
@owner_required
def index():
    jobs      = Job.query.filter_by(company_id=current_user.company_id).all()
    conflicts = detect_conflicts(current_user.company_id)
    overdue   = [j for j in jobs if not j.invoice_sent and j.status == 'complete']
    return render_template('index.html', jobs=jobs, conflicts=conflicts, overdue=overdue)


@app.route('/calendar')
@login_required
@owner_required
def calendar():
    jobs      = Job.query.filter_by(company_id=current_user.company_id).all()
    conflicts = detect_conflicts(current_user.company_id)
    return render_template('calendar.html', jobs=jobs, conflicts=conflicts)


@app.route('/job-brief')
@login_required
@owner_required
def job_brief():
    return render_template('job_brief.html')


@app.route('/invoice')
@login_required
@owner_required
def invoice():
    return render_template('invoice.html')

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
    return render_template('employee.html', jobs=jobs)

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
        'color':     platform_color(j.platform)
    } for j in jobs])


@app.route('/api/jobs', methods=['POST'])
@login_required
@owner_required
def add_job():
    data = request.json
    job = Job(
        company_id    = current_user.company_id,
        title         = data['title'],
        platform      = data.get('platform', 'manual'),
        location      = data.get('location', ''),
        start_time    = datetime.fromisoformat(data['start']),
        end_time      = datetime.fromisoformat(data['end']),
        tech_assigned = data.get('tech', ''),
        tech_pay      = data.get('tech_pay'),
        job_pay       = data.get('job_pay'),
        notes         = data.get('notes', '')
    )
    db.session.add(job)
    db.session.commit()
    detect_and_save_conflicts(current_user.company_id)
    return jsonify({'success': True, 'id': job.id})


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
    return jsonify({'success': True})

# ─────────────────────────────────────────
# SETTINGS ROUTES
# ─────────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
@login_required
@owner_required
def settings():
    creds = {
        c.platform: c
        for c in PlatformCredential.query.filter_by(company_id=current_user.company_id).all()
    }
    if request.method == 'POST':
        for platform in ('workmarket', 'fieldnation'):
            api_key    = request.form.get(f'{platform}_key', '').strip()
            api_secret = request.form.get(f'{platform}_secret', '').strip()
            enabled    = request.form.get(f'{platform}_enabled') == 'on'
            if platform in creds:
                creds[platform].api_key    = api_key
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
    return render_template('settings.html', creds=creds, company=current_user.company)


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def platform_color(platform):
    return {
        'workmarket':  '#2563eb',
        'fieldnation': '#16a34a',
        'manual':      '#9333ea'
    }.get(platform, '#6b7280')


def detect_conflicts(company_id):
    jobs = Job.query.filter_by(company_id=company_id).order_by(Job.start_time).all()
    conflicts = []
    for i in range(len(jobs)):
        for j in range(i + 1, len(jobs)):
            a, b = jobs[i], jobs[j]
            if a.start_time < b.end_time and b.start_time < a.end_time:
                conflicts.append({
                    'job_a': a.title, 'job_b': b.title,
                    'job_a_id': a.id, 'job_b_id': b.id,
                    'start_a': a.start_time.isoformat(),
                    'start_b': b.start_time.isoformat()
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
# INIT
# ─────────────────────────────────────────

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        print("FieldBase database initialized.")
    app.run(debug=True, port=5050)
