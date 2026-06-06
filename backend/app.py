from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from datetime import datetime
from functools import wraps
from sqlalchemy import text
import bcrypt
import os
import smtplib
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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
    invoice_sent      = db.Column(db.Boolean, default=False)
    invoice_sent_at   = db.Column(db.DateTime)
    payment_received  = db.Column(db.Boolean, default=False)
    amount_paid       = db.Column(db.Float)
    notes             = db.Column(db.Text)
    client_name       = db.Column(db.String(200))
    client_email      = db.Column(db.String(200))
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)


class JobPhoto(db.Model):
    __tablename__ = 'job_photos'
    id          = db.Column(db.Integer, primary_key=True)
    job_id      = db.Column(db.Integer, db.ForeignKey('jobs.id'), nullable=False)
    company_id  = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    filename    = db.Column(db.String(300), nullable=False)
    uploaded_by = db.Column(db.String(200))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


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

@app.after_request
def no_cache(response):
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


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
def index():
    if not current_user.is_authenticated:
        return render_template('landing.html')
    if current_user.role == 'employee':
        return redirect(url_for('employee_dashboard'))
    from datetime import date
    jobs        = Job.query.filter_by(company_id=current_user.company_id).order_by(Job.start_time).all()
    conflicts   = detect_conflicts(current_user.company_id)
    today       = [j for j in jobs if j.start_time.date() == date.today()]
    overdue     = [j for j in jobs if not j.invoice_sent and j.status == 'complete']
    unconfirmed = [j for j in jobs if j.tech_assigned and not j.tech_confirmed and j.status == 'scheduled']
    active      = [j for j in jobs if j.status == 'in_progress']
    return render_template('index.html',
        jobs=jobs, conflicts=conflicts, overdue=overdue,
        today=today, unconfirmed=unconfirmed, active=active,
        company=current_user.company)


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
    employees = User.query.filter_by(
        company_id=current_user.company_id,
        role='employee',
        is_active=True
    ).order_by(User.name).all()
    return render_template('job_brief.html', owner=current_user, employees=employees)


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
        notes         = data.get('notes', ''),
        client_name   = data.get('client_name', ''),
        client_email  = data.get('client_email', ''),
    )
    db.session.add(job)
    db.session.commit()
    detect_and_save_conflicts(current_user.company_id)

    # Notify assigned employee by email
    if job.tech_assigned:
        emp = User.query.filter_by(
            company_id=current_user.company_id,
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

    return jsonify({'success': True, 'id': job.id})


@app.route('/api/jobs/<int:job_id>', methods=['DELETE'])
@login_required
@owner_required
def delete_job(job_id):
    job = Job.query.filter_by(id=job_id, company_id=current_user.company_id).first_or_404()
    Conflict.query.filter(
        (Conflict.job_a_id == job_id) | (Conflict.job_b_id == job_id)
    ).delete(synchronize_session=False)
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
    color_map = {'scheduled': '#f59e0b', 'confirmed': '#3b82f6', 'in_progress': '#8b5cf6', 'complete': '#22c55e'}
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
    """Get a job that belongs to the current employee's company."""
    return Job.query.filter_by(id=job_id, company_id=current_user.company_id).first_or_404()

@app.route('/api/jobs/<int:job_id>/confirm', methods=['POST'])
@login_required
def confirm_job(job_id):
    job = _employee_job(job_id)
    job.tech_confirmed = True
    job.confirmed_at   = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/jobs/<int:job_id>/clock-in', methods=['POST'])
@login_required
def clock_in(job_id):
    job = _employee_job(job_id)
    job.clock_in_at = datetime.utcnow()
    job.status      = 'in_progress'
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/jobs/<int:job_id>/clock-out', methods=['POST'])
@login_required
def clock_out(job_id):
    job = _employee_job(job_id)
    job.clock_out_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/jobs/<int:job_id>/complete', methods=['POST'])
@login_required
def complete_job(job_id):
    job = _employee_job(job_id)
    job.status       = 'complete'
    job.completed_at = datetime.utcnow()
    if not job.clock_out_at:
        job.clock_out_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})

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


# ─────────────────────────────────────────
# PHOTO ROUTES
# ─────────────────────────────────────────

UPLOAD_FOLDER = os.path.join(_ROOT, 'frontend', 'static', 'uploads', 'photos')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
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
    f.save(os.path.join(UPLOAD_FOLDER, filename))
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
        'url':         f'/static/uploads/photos/{p.filename}',
        'uploaded_by': p.uploaded_by,
        'uploaded_at': p.uploaded_at.strftime('%b %d %I:%M %p')
    } for p in photos])


@app.route('/api/photos/<int:photo_id>', methods=['DELETE'])
@login_required
@owner_required
def delete_photo(photo_id):
    photo = JobPhoto.query.filter_by(id=photo_id, company_id=current_user.company_id).first_or_404()
    try:
        os.remove(os.path.join(UPLOAD_FOLDER, photo.filename))
    except FileNotFoundError:
        pass
    db.session.delete(photo)
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
    data         = request.json or {}
    client_email = data.get('client_email') or job.client_email
    client_name  = data.get('client_name')  or job.client_name or 'Client'
    if not client_email:
        return jsonify({'error': 'No client email provided'}), 400
    if data.get('client_email'):
        job.client_email = client_email
    if data.get('client_name'):
        job.client_name = client_name
    job.invoice_sent    = True
    job.invoice_sent_at = datetime.utcnow()
    db.session.commit()

    amount = f"${job.job_pay:.2f}" if job.job_pay else 'See attached'
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
      <div style="background:#1e3a5f;padding:24px 32px;">
        <h1 style="color:#fff;margin:0;font-size:22px;">{current_user.company.name}</h1>
        <p style="color:#a8c4e0;margin:4px 0 0;font-size:13px;">Invoice</p>
      </div>
      <div style="padding:32px;">
        <p style="color:#374151;">Dear {client_name},</p>
        <p style="color:#374151;">Please find your invoice details below for the recently completed work.</p>
        <table style="width:100%;border-collapse:collapse;margin:24px 0;">
          <tr style="background:#f9fafb;"><td style="padding:10px 14px;font-size:13px;font-weight:600;color:#6b7280;text-transform:uppercase;">Job</td><td style="padding:10px 14px;font-size:14px;color:#1f2937;">{job.title}</td></tr>
          <tr><td style="padding:10px 14px;font-size:13px;font-weight:600;color:#6b7280;text-transform:uppercase;">Date</td><td style="padding:10px 14px;font-size:14px;color:#1f2937;">{job.start_time.strftime('%B %d, %Y')}</td></tr>
          <tr style="background:#f9fafb;"><td style="padding:10px 14px;font-size:13px;font-weight:600;color:#6b7280;text-transform:uppercase;">Location</td><td style="padding:10px 14px;font-size:14px;color:#1f2937;">{job.location or 'N/A'}</td></tr>
          <tr><td style="padding:10px 14px;font-size:13px;font-weight:600;color:#6b7280;text-transform:uppercase;">Amount Due</td><td style="padding:10px 14px;font-size:18px;font-weight:700;color:#1e3a5f;">{amount}</td></tr>
        </table>
        <p style="color:#6b7280;font-size:13px;">Please remit payment at your earliest convenience. Thank you for your business.</p>
        <p style="color:#374151;margin-top:24px;">— {current_user.name}<br>{current_user.company.name}</p>
      </div>
    </div>"""
    ok = send_email(client_email, f'Invoice — {job.title}', html)
    return jsonify({'success': ok, 'sent_to': client_email})


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
# INIT — runs on every startup (Gunicorn + direct)
# ─────────────────────────────────────────

with app.app_context():
    db.create_all()
    with db.engine.connect() as conn:
        for col, ddl in [
            ('payment_received', 'ALTER TABLE jobs ADD COLUMN payment_received BOOLEAN DEFAULT FALSE'),
            ('amount_paid',      'ALTER TABLE jobs ADD COLUMN amount_paid FLOAT'),
            ('hourly_rate',      'ALTER TABLE users ADD COLUMN hourly_rate FLOAT'),
            ('client_name',      'ALTER TABLE jobs ADD COLUMN client_name VARCHAR(200)'),
            ('client_email',     'ALTER TABLE jobs ADD COLUMN client_email VARCHAR(200)'),
        ]:
            try:
                conn.execute(text(ddl))
                conn.commit()
            except Exception:
                conn.rollback()

if __name__ == '__main__':
    app.run(debug=True, port=5050)
