from flask import Flask, render_template, jsonify, request, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import os

app = Flask(__name__, template_folder='../frontend/templates', static_folder='../frontend/static')
app.secret_key = 'fieldbase_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///../database/fieldbase.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ─────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────

class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    platform = db.Column(db.String(50), nullable=False)  # workmarket, fieldnation, manual
    location = db.Column(db.String(300))
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(50), default='scheduled')  # scheduled, in_progress, complete
    tech_assigned = db.Column(db.String(100))
    tech_confirmed = db.Column(db.Boolean, default=False)
    invoice_sent = db.Column(db.Boolean, default=False)
    invoice_sent_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)

class Conflict(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_a_id = db.Column(db.Integer, db.ForeignKey('job.id'))
    job_b_id = db.Column(db.Integer, db.ForeignKey('job.id'))
    detected_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved = db.Column(db.Boolean, default=False)

# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/calendar')
def calendar():
    jobs = Job.query.all()
    conflicts = detect_conflicts()
    return render_template('calendar.html', jobs=jobs, conflicts=conflicts)

@app.route('/job-brief')
def job_brief():
    return render_template('job_brief.html')

@app.route('/invoice')
def invoice():
    return render_template('invoice.html')

@app.route('/api/jobs')
def get_jobs():
    jobs = Job.query.all()
    return jsonify([{
        'id': j.id,
        'title': j.title,
        'platform': j.platform,
        'location': j.location,
        'start': j.start_time.isoformat(),
        'end': j.end_time.isoformat(),
        'status': j.status,
        'tech': j.tech_assigned,
        'confirmed': j.tech_confirmed,
        'color': platform_color(j.platform)
    } for j in jobs])

@app.route('/api/jobs', methods=['POST'])
def add_job():
    data = request.json
    job = Job(
        title=data['title'],
        platform=data.get('platform', 'manual'),
        location=data.get('location', ''),
        start_time=datetime.fromisoformat(data['start']),
        end_time=datetime.fromisoformat(data['end']),
        tech_assigned=data.get('tech', ''),
        notes=data.get('notes', '')
    )
    db.session.add(job)
    db.session.commit()
    detect_and_save_conflicts()
    return jsonify({'success': True, 'id': job.id})

@app.route('/api/conflicts')
def get_conflicts():
    conflicts = detect_conflicts()
    return jsonify(conflicts)

@app.route('/api/invoice/<int:job_id>/sent', methods=['POST'])
def mark_invoice_sent(job_id):
    job = Job.query.get_or_404(job_id)
    job.invoice_sent = True
    job.invoice_sent_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def platform_color(platform):
    colors = {
        'workmarket': '#2563eb',   # blue
        'fieldnation': '#16a34a',  # green
        'manual': '#9333ea'        # purple
    }
    return colors.get(platform, '#6b7280')

def detect_conflicts():
    jobs = Job.query.order_by(Job.start_time).all()
    conflicts = []
    for i in range(len(jobs)):
        for j in range(i + 1, len(jobs)):
            a, b = jobs[i], jobs[j]
            if a.start_time < b.end_time and b.start_time < a.end_time:
                conflicts.append({
                    'job_a': a.title,
                    'job_b': b.title,
                    'job_a_id': a.id,
                    'job_b_id': b.id,
                    'start_a': a.start_time.isoformat(),
                    'start_b': b.start_time.isoformat()
                })
    return conflicts

def detect_and_save_conflicts():
    conflicts = detect_conflicts()
    # Clear old unresolved conflicts
    Conflict.query.filter_by(resolved=False).delete()
    for c in conflicts:
        conflict = Conflict(job_a_id=c['job_a_id'], job_b_id=c['job_b_id'])
        db.session.add(conflict)
    db.session.commit()

# ─────────────────────────────────────────
# INIT
# ─────────────────────────────────────────

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        print("FieldBase database initialized.")
    app.run(debug=True, port=5050)
