import os
import json
import datetime
import time
from collections import defaultdict
from flask import Flask, render_template, request, redirect, url_for, session, flash
import gspread
from google.oauth2.service_account import Credentials
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-key-change-in-production')

SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
ADMIN_PASSWORD_HASH = generate_password_hash(os.environ.get('ADMIN_PASSWORD', 'change-me'))

# 10-second in-memory cache
_cache = {}
_CACHE_TTL = 10

def get_gspread_client():
    creds_dict = json.loads(os.environ.get('GOOGLE_APPLICATION_CREDENTIALS_JSON'))
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    )
    return gspread.authorize(creds)

def _cached_load(func_name, loader_func):
    now = time.time()
    if func_name not in _cache or now - _cache[func_name]['time'] > _CACHE_TTL:
        print(f"[{datetime.datetime.now()}] DEBUG: Cache miss for {func_name} – reloading from Google Sheets")
        _cache[func_name] = {'data': loader_func(), 'time': now}
    else:
        print(f"[{datetime.datetime.now()}] DEBUG: Cache hit for {func_name}")
    return _cache[func_name]['data']

def load_precincts():
    def _load():
        gc = get_gspread_client()
        sheet = gc.open_by_key(SPREADSHEET_ID).worksheet('Precincts')
        records = sheet.get_all_records()
        return {str(r['Precinct']): int(r.get('Allotted', 1)) for r in records}
    return _cached_load('precincts', _load)

def load_delegates():
    def _load():
        gc = get_gspread_client()
        sheet = gc.open_by_key(SPREADSHEET_ID).worksheet('Delegates')
        records = sheet.get_all_records()
        seated_count = defaultdict(int)
        delegate_list = []
        for r in records:
            present = str(r.get('Present/Not Present', '')).strip().lower()
            if present in ['present', 'yes', '1', 'true', 'y']:
                seated_count[str(r.get('Precinct', 'Unknown'))] += 1
                delegate_list.append(r)
        precincts = load_precincts()
        delegates = {}
        for r in delegate_list:
            precinct = str(r.get('Precinct', 'Unknown'))
            allotted = precincts.get(precinct, 1)
            count = seated_count[precinct]
            strength = round(allotted / count, 4) if count > 0 else 1.0
            first = r.get('First Name', '').strip()
            last = r.get('Last Name', '').strip()
            name = f"{first} {last}".strip()
            vuid = str(r.get('VUID', '')).strip()
            key = vuid if vuid else f"{name} ({precinct})"
            delegates[key] = {
                'Name': name, 'Precinct': precinct, 'VUID': vuid,
                'Strength': strength, 'Key': key,
                'Display': f"{name} ({precinct}) – strength {strength}"
            }
        print(f"[{datetime.datetime.now()}] DEBUG: Loaded {len(delegates)} seated delegates")
        return delegates
    return _cached_load('delegates', _load)

def get_polls():
    def _load():
        gc = get_gspread_client()
        sheet = gc.open_by_key(SPREADSHEET_ID).worksheet('Polls')
        records = sheet.get_all_records()
        return {str(r['PollID']): r for r in records if r.get('PollID')}
    return _cached_load('polls', _load)

def get_votes():
    def _load():
        gc = get_gspread_client()
        sheet = gc.open_by_key(SPREADSHEET_ID).worksheet('Votes')
        return sheet.get_all_records()
    return _cached_load('votes', _load)

def record_vote(poll_id, delegate_key, option):
    print(f"[{datetime.datetime.now()}] DEBUG: record_vote called – poll={poll_id}, delegate_key={delegate_key}, option={option}")
    try:
        gc = get_gspread_client()
        votes_sheet = gc.open_by_key(SPREADSHEET_ID).worksheet('Votes')
        current_votes = votes_sheet.get_all_records()
        print(f"[{datetime.datetime.now()}] DEBUG: Read {len(current_votes)} existing votes from sheet")

        delegates = load_delegates()
        delegate = delegates.get(delegate_key)
        if not delegate:
            print(f"[{datetime.datetime.now()}] DEBUG: Delegate key '{delegate_key}' not found")
            return False, "Delegate not found or not seated"

        print(f"[{datetime.datetime.now()}] DEBUG: Found delegate: {delegate['Name']} (key={delegate_key})")

        # Duplicate check
        existing = [v for v in current_votes
                    if str(v.get('PollID', '')) == str(poll_id)
                    and str(v.get('DelegateKey', '')) == str(delegate_key)]
        if existing:
            print(f"[{datetime.datetime.now()}] DEBUG: Duplicate vote detected for this delegate")
            return False, f"❌ {delegate['Name']} has already voted on this poll"

        # Record vote
        print(f"[{datetime.datetime.now()}] DEBUG: Appending new vote row to Votes tab...")
        votes_sheet.append_row([
            len(current_votes) + 1,
            poll_id,
            delegate['Name'],
            delegate['Precinct'],
            delegate['VUID'],
            delegate_key,
            option,
            datetime.datetime.now().isoformat(),
            delegate['Strength']
        ])
        print(f"[{datetime.datetime.now()}] DEBUG: Vote successfully appended to Google Sheet")

        if 'votes' in _cache:
            del _cache['votes']
            print(f"[{datetime.datetime.now()}] DEBUG: Votes cache cleared")

        return True, f"✅ Vote recorded for {delegate['Name']} – {option} (strength {delegate['Strength']})"

    except Exception as e:
        error_msg = f"❌ Vote failed: {str(e)}"
        print(f"[{datetime.datetime.now()}] VOTE ERROR: {error_msg}")
        return False, error_msg

def calculate_results(poll_id):
    votes = get_votes()
    results = {}
    for v in votes:
        if str(v.get('PollID', '')) == str(poll_id):
            opt = v.get('OptionChosen')
            if opt:
                results[opt] = results.get(opt, 0) + float(v.get('Strength', 0))
    return results

# ====================== ROUTES ======================
@app.route('/')
def public_results():
    print(f"[{datetime.datetime.now()}] DEBUG: Public results page loaded")
    polls = get_polls()
    active_polls = {pid: p for pid, p in polls.items() if p.get('Active')}
    results = {pid: calculate_results(pid) for pid in active_polls}
    return render_template('public.html', polls=active_polls, results=results)

@app.route('/vote', methods=['GET', 'POST'])
def vote():
    if request.method == 'POST':
        print(f"[{datetime.datetime.now()}] DEBUG: POST to /vote – identifier={request.form.get('identifier')}")
    delegates = load_delegates()
    polls = {pid: p for pid, p in get_polls().items() if p.get('Active')}
    
    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        poll_id = request.form.get('poll_id')
        option = request.form.get('option')
        
        delegate_key = None
        for k, d in delegates.items():
            if (k.lower() == identifier.lower() or
                d['Name'].lower() == identifier.lower() or
                (d.get('VUID') and d['VUID'].lower() == identifier.lower())):
                delegate_key = k
                break
        
        if not delegate_key:
            flash('Delegate not found or not present.', 'danger')
            return redirect(url_for('vote'))
        
        success, msg = record_vote(poll_id, delegate_key, option)
        flash(msg, 'success' if success else 'warning')
        return redirect(url_for('public_results'))
    
    return render_template('vote.html', delegates=delegates, polls=polls)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        print(f"[{datetime.datetime.now()}] DEBUG: Admin login attempt")
    if request.method == 'POST':
        if check_password_hash(ADMIN_PASSWORD_HASH, request.form.get('password')):
            session['admin'] = True
            return redirect(url_for('admin'))
        flash('Wrong password', 'danger')
    return render_template('admin_login.html')

@app.route('/admin', methods=['GET', 'POST'])
@app.route('/admin/<action>', methods=['POST'])
def admin(action=None):
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    
    delegates = load_delegates()
    polls = get_polls()
    votes = get_votes()
    
    if request.method == 'POST':
        print(f"[{datetime.datetime.now()}] DEBUG: POST to admin – action={action}")
        gc = get_gspread_client()
        polls_sheet = gc.open_by_key(SPREADSHEET_ID).worksheet('Polls')
        
        if action == 'create':
            title = request.form['title']
            desc = request.form.get('description', '')
            options_str = request.form['options']
            options = json.dumps([o.strip() for o in options_str.split(',')])
            polls_sheet.append_row([len(polls) + 1, title, desc, options, True])
            flash('Poll created!', 'success')
        
        elif action == 'toggle':
            poll_id = int(request.form['poll_id'])
            records = polls_sheet.get_all_records()
            for i, row in enumerate(records):
                if int(row['PollID']) == poll_id:
                    new_active = not bool(row.get('Active'))
                    polls_sheet.update_cell(i + 2, 5, new_active)
                    break
            flash('Poll toggled!', 'info')
        
        elif action == 'manual_vote':
            poll_id = request.form['poll_id']
            delegate_key = request.form['delegate_key']
            option = request.form['option']
            success, msg = record_vote(poll_id, delegate_key, option)
            flash(msg, 'success' if success else 'warning')
        
        return redirect(url_for('admin'))
    
    return render_template('admin.html', polls=polls, votes=votes, delegates=delegates)

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    return redirect(url_for('public_results'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)