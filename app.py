from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from urllib.parse import urlparse, urljoin
from functools import wraps
import json
import os
import urllib.request
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from werkzeug.utils import secure_filename

# app.py はプロジェクト直下に置く。
# 実体（templates / static / data）は bousai_app/ 配下にあるので、そこを参照する。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, 'bousai_app')

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, 'templates'),
    static_folder=os.path.join(APP_DIR, 'static'),
)
app.secret_key = 'your-secret-key-here'

# 管理者認証情報
ADMIN_CREDENTIALS = {
    'admin': '123'
}

# ────────────────────────────────
# 気象警報・注意報設定
PREFECTURE_CODE = "020000"  # 青森県
AREA_NAME = "青森市"

# 気象庁の市区町村コード（青森市）
AREA_CODE = "0220100"

WARNING_URL = (
    f"https://www.jma.go.jp/bosai/warning/data/r8/{PREFECTURE_CODE}.json"
)

JST = timezone(timedelta(hours=9))

# 警報・注意報のコード一覧
WARNING_CODES = {
    "00": "解除",
    "02": "暴風雪警報",
    "03": "レベル3大雨警報",
    "04": "洪水警報",
    "05": "暴風警報",
    "06": "大雪警報",
    "07": "波浪警報",
    "08": "レベル3高潮警報",
    "09": "レベル3土砂災害警報",
    "10": "レベル2大雨注意報",
    "12": "大雪注意報",
    "13": "風雪注意報",
    "14": "雷注意報",
    "15": "強風注意報",
    "16": "波浪注意報",
    "17": "融雪注意報",
    "18": "洪水注意報",
    "19": "レベル2高潮注意報",
    "20": "濃霧注意報",
    "21": "乾燥注意報",
    "22": "なだれ注意報",
    "23": "低温注意報",
    "24": "霜注意報",
    "25": "着氷注意報",
    "26": "着雪注意報",
    "27": "その他の注意報",
    "29": "レベル2土砂災害注意報",
    "32": "暴風雪特別警報",
    "33": "レベル5大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "レベル5高潮特別警報",
    "39": "レベル5土砂災害特別警報",
    "43": "レベル4大雨危険警報",
    "48": "レベル4高潮危険警報",
    "49": "レベル4土砂災害危険警報"
}

# ────────────────────────────────
# サンプルデータの読み込み
DATA_FILE = os.path.join(APP_DIR, 'data', 'shelters.json')
INSTRUCTIONS_FILE = os.path.join(APP_DIR, 'data', 'instructions.json')
CROWD_FILE = os.path.join(APP_DIR, 'data', 'crowd_reports.json')

def load_json(path, default):
    """JSONファイルを読み込む（存在しない・壊れている場合は default を返す）"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

shelters = load_json(DATA_FILE, [])
instructions = load_json(INSTRUCTIONS_FILE, [])
crowd_reports = load_json(CROWD_FILE, [])

DISASTER_OPTIONS = ['地震', '津波', '洪水', '土砂災害', '台風・大雨', '火災']
ACCESSIBILITY_OPTIONS = ['車いす対応', 'スロープ', '点字案内', '音声案内', '多目的トイレ']
OPEN_STATUS_OPTIONS = ['開設中', '開設予定', '閉鎖中']
PET_OPTIONS = ['可', '条件付き', '不可']
CROWD_OPTIONS = ['空', '混', '満']
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
UPLOAD_DIR = os.path.join(APP_DIR, 'static', 'uploads')
REGISTER_DISASTER_OPTIONS = DISASTER_OPTIONS + ['その他']
REGISTER_FACILITY_OPTIONS = ['飲料水', '食料', '毛布', 'トイレ', '駐車場', '乳幼児対応', 'その他']
REGISTER_ACCESSIBILITY_OPTIONS = ACCESSIBILITY_OPTIONS + ['その他']

def save_crowd_reports():
    """混雑投稿履歴をファイルに保存する"""
    try:
        with open(CROWD_FILE, 'w', encoding='utf-8') as f:
            json.dump(crowd_reports, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def normalize_shelter(shelter):
    """旧形式の避難所データにも表示用の既定値を補う"""
    normalized = dict(shelter)
    for key in ('disasters', 'accessibility'):
        value = normalized.get(key, [])
        normalized[key] = value if isinstance(value, list) else [value]
    for key in ('address', 'area', 'facilities', 'contact', 'opening_status', 'pet_policy'):
        normalized.setdefault(key, '')
    normalized.setdefault('latitude', None)
    normalized.setdefault('longitude', None)
    if (normalized['latitude'] is None or normalized['longitude'] is None) and AREA_NAME in normalized.get('address', ''):
        # 旧データに座標がない場合は対象地域の代表地点を表示する。
        normalized['latitude'] = 40.8244
        normalized['longitude'] = 140.7400
    district = normalized.get('district') or normalized.get('area', '')
    if not district and AREA_NAME in normalized.get('address', ''):
        district = AREA_NAME
    normalized['district'] = district
    normalized.setdefault('available', None)
    normalized.setdefault('infant', False)
    normalized.setdefault('parking', False)
    return normalized

def parse_posted_at(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(JST)
    except (AttributeError, ValueError):
        return None

def crowd_summary(shelter_id):
    """直近3時間の混雑投稿を集計し、30日超の履歴を整理する"""
    now = datetime.now(JST)
    active = []
    retained = []
    for report in crowd_reports:
        posted_at = parse_posted_at(report.get('posted_at'))
        if not posted_at:
            retained.append(report)
            continue
        if now - posted_at <= timedelta(days=30):
            retained.append(report)
        if report.get('shelter_id') == shelter_id and now - posted_at <= timedelta(hours=3):
            if report.get('status') in CROWD_OPTIONS:
                active.append((report, posted_at))
    if len(retained) != len(crowd_reports):
        crowd_reports[:] = retained
        save_crowd_reports()
    counts = {status: 0 for status in CROWD_OPTIONS}
    for report, _ in active:
        counts[report['status']] += 1
    latest_by_status = {status: max((posted for report, posted in active if report['status'] == status), default=None)
                        for status in CROWD_OPTIONS}
    latest = max((posted for _, posted in active), default=None)
    if active:
        status = max(CROWD_OPTIONS, key=lambda item: (counts[item], latest_by_status[item] or datetime.min.replace(tzinfo=JST)))
    else:
        status = '情報なし'
    return {
        'status': status,
        'counts': counts,
        'total': len(active),
        'latest': latest.strftime('%Y年%m月%d日 %H:%M') if latest else None,
    }

def enrich_shelter(shelter):
    normalized = normalize_shelter(shelter)
    normalized['crowd'] = crowd_summary(normalized.get('id'))
    return normalized

def save_instructions():
    """指示ボードのデータをファイルに保存する"""
    try:
        with open(INSTRUCTIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(instructions, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def save_shelters():
    """避難所データをファイルに保存する"""
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(shelters, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def recent_shelters():
    """登録日時の新しい避難所を上位から返す"""
    return sorted(shelters, key=lambda item: item.get('latest_updated_at', ''), reverse=True)[:5]

def register_form_context(form=None, errors=None, success=None):
    return {
        'form': form or {},
        'errors': errors or [],
        'success': success,
        'disaster_options': REGISTER_DISASTER_OPTIONS,
        'facility_options': REGISTER_FACILITY_OPTIONS,
        'accessibility_options': REGISTER_ACCESSIBILITY_OPTIONS,
        'opening_status_options': OPEN_STATUS_OPTIONS,
        'pet_options': PET_OPTIONS,
        'recent_shelters': recent_shelters(),
    }

def validate_registration(form):
    errors = []
    name = form.get('name', '').strip()
    postal_code = re.sub(r'[-ー]', '', form.get('postal_code', '').strip())
    address = form.get('address', '').strip()
    phone = form.get('phone', '').strip()
    email = form.get('email', '').strip()
    status = form.get('status', '').strip()
    capacity = form.get('capacity', '').strip()
    disasters = form.getlist('disasters')
    pet_policy = form.get('pet_policy', '').strip()
    if not name or not postal_code or not address or not status or not disasters or not pet_policy:
        errors.append('未入力の必須項目があります。確認してください。')
    if postal_code and not re.fullmatch(r'\d{7}', postal_code):
        errors.append('郵便番号は7桁の数字で入力してください。')
    if phone and not re.fullmatch(r'[0-9０-９+()（）\-ー ]{8,20}', phone):
        errors.append('電話番号の形式が正しくありません。')
    if email and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        errors.append('メールアドレスの形式が正しくありません。')
    if capacity and (not capacity.isdigit() or int(capacity) < 0):
        errors.append('最大収容人数は0以上の整数で入力してください。')
    if status and status not in OPEN_STATUS_OPTIONS:
        errors.append('開設状況の選択が正しくありません。')
    if pet_policy and pet_policy not in PET_OPTIONS:
        errors.append('ペット同行避難の選択が正しくありません。')
    return errors

def save_uploaded_image(file):
    if not file or not file.filename:
        return None
    filename = secure_filename(file.filename)
    extension = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError('施設画像はpng、jpg、jpeg、gif、webp形式のみ対応しています。')
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    stored_name = f'{uuid.uuid4().hex}.{extension}'
    file.save(os.path.join(UPLOAD_DIR, stored_name))
    return url_for('static', filename=f'uploads/{stored_name}')

def registration_payload(form, image_url=None):
    def coordinate_value(field):
        value = form.get(field, '').strip()
        try:
            return float(value) if value else None
        except ValueError:
            return None

    payload = {
        'name': form.get('name', '').strip(),
        'district': form.get('district', '').strip() or AREA_NAME,
        'area': form.get('area', '').strip() or AREA_NAME,
        'postal_code': re.sub(r'[-ー]', '', form.get('postal_code', '').strip()),
        'address': form.get('address', '').strip(),
        'latitude': coordinate_value('latitude'),
        'longitude': coordinate_value('longitude'),
        'phone': form.get('phone', '').strip(),
        'email': form.get('email', '').strip(),
        'status': form.get('status', '').strip(),
        'capacity': int(form.get('capacity')) if form.get('capacity', '').isdigit() else None,
        'disasters': form.getlist('disasters'),
        'disaster_other': form.get('disaster_other', '').strip(),
        'facilities': form.getlist('facilities'),
        'facility_other': form.get('facility_other', '').strip(),
        'accessibility': form.getlist('accessibility'),
        'accessibility_other': form.get('accessibility_other', '').strip(),
        'pet_policy': form.get('pet_policy', '').strip(),
        'latest_updated_at': form.get('latest_updated_at', '').strip() or datetime.now(JST).isoformat(),
        'image': image_url or form.get('image', ''),
    }
    return payload
# ────────────────────────────────

# ────────────────────────────────
# 認証関連の設定とヘルパー関数
def is_safe_url(target):
    """リダイレクト先URLが安全かどうかチェック"""
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

def login_required(f):
    """認証が必要なページに付けるデコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            # 現在のURLをnextパラメータとしてログイン画面にリダイレクト
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def get_japan_time():
    """日本時間（JST）の現在時刻を取得する"""
    return datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")


def format_report_time(iso_str):
    """気象庁の発表時刻（ISO形式）をJSTの表示用文字列に変換する"""
    if not iso_str:
        return "不明"
    try:
        parsed = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(JST)
        return parsed.strftime("%Y年%m月%d日 %H:%M")
    except ValueError:
        return iso_str


def filter_shelters(district=None, filters=None):
    """地区と検索チェックボックスで避難所を絞り込む"""
    filters = filters or set()
    filtered = []
    for shelter in shelters:
        normalized = normalize_shelter(shelter)
        shelter_district = normalized.get('district') or normalized.get('area', '')
        if district and shelter_district != district:
            continue
        if 'available' in filters:
            crowd = crowd_summary(normalized.get('id'))
            if normalized.get('available') is not True and crowd['status'] != '空':
                continue
        if 'barrier_free' in filters and not normalized.get('accessibility'):
            continue
        if 'pets' in filters and normalized.get('pet_policy') != '可':
            continue
        facilities = str(normalized.get('facilities', ''))
        if 'infant' in filters and not (normalized.get('infant') is True or '乳幼児' in facilities):
            continue
        if 'parking' in filters and not (normalized.get('parking') is True or '駐車場' in facilities):
            continue
        filtered.append(shelter)
    return filtered


def parse_area_warnings(warning_data):
    """気象庁の新形式JSONから対象市区町村の発表・継続中の情報を抽出する"""
    if not isinstance(warning_data, list):
        raise ValueError("気象庁の警報・注意報データが新形式の配列ではありません")

    warnings = []
    seen_codes = set()
    report_datetimes = []

    for report in warning_data:
        if not isinstance(report, dict):
            continue

        report_datetime = report.get("reportDatetime")
        if isinstance(report_datetime, str) and report_datetime:
            report_datetimes.append(report_datetime)

        warning = report.get("warning")
        if not isinstance(warning, dict):
            continue

        class20_items = warning.get("class20Items", [])
        if not isinstance(class20_items, list):
            continue

        area = next(
            (
                item for item in class20_items
                if isinstance(item, dict)
                and item.get("areaCode") == AREA_CODE
            ),
            None
        )
        if not area:
            continue

        kinds = area.get("kinds", [])
        if not isinstance(kinds, list):
            continue

        for kind in kinds:
            if not isinstance(kind, dict):
                continue

            status = kind.get("status", "")
            code = kind.get("code", "")
            if status not in ("発表", "継続") or not code or code in seen_codes:
                continue

            warnings.append({
                "name": WARNING_CODES.get(
                    code,
                    f"不明な警報・注意報 (コード: {code})"
                ),
                "code": code,
                "status": status
            })
            seen_codes.add(code)

    latest_report_datetime = max(report_datetimes, default="")
    return warnings, latest_report_datetime


def get_weather_warnings():
    """対象市区町村の警報・注意報を取得する"""
    try:
        # 青森県の新形式（令和8年～）警報・注意報データを取得
        with urllib.request.urlopen(url=WARNING_URL, timeout=10) as res:
            warning_data = json.loads(res.read())

        warnings, report_datetime = parse_area_warnings(warning_data)

        return {
            "area_name": AREA_NAME,
            "warnings": warnings,
            "report_time": format_report_time(report_datetime),
            "last_fetch_time": get_japan_time()
        }

    except Exception:
        return {
            "area_name": AREA_NAME,
            "warnings": [],
            "report_time": "取得失敗",
            "last_fetch_time": get_japan_time(),
            "error": True
        }


# トップページ：templates/index.html を返す（住民向け指示も表示する）
@app.route('/')
def index():
    resident_notices = [i for i in instructions if i.get('target') == '住民']
    return render_template(
        'index.html',
        resident_notices=resident_notices,
        map_shelters=[normalize_shelter(s) for s in shelters],
    )

# ログインページ
@app.route('/login', methods=['GET', 'POST'])
def login():
    # リダイレクト先を取得（デフォルトはホーム画面）
    next_url = request.args.get('next') or request.form.get('next')

    # 安全でないURLの場合はデフォルトページにリダイレクト
    if not next_url or not is_safe_url(next_url):
        next_url = url_for('index')

    if request.method == 'POST':
        password = request.form.get('password', '').strip()

        # 認証チェック
        username = next(
            (name for name, registered_password in ADMIN_CREDENTIALS.items()
             if registered_password == password),
            None
        )
        if username:
            session['logged_in'] = True
            session['username'] = username
            # ログイン成功後は指定されたページにリダイレクト
            return redirect(next_url)
        return render_template('login.html', error=True, message="パスワードが正しくありません。", next=next_url)

    # ログイン済みの場合は指定されたページにリダイレクト
    if session.get('logged_in'):
        return redirect(next_url)

    return render_template('login.html', next=next_url)

# ログアウト
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# 避難所登録ページ※user が避難所登録ページについて具体的に修正指示しない限り、このコードは正しいのでこのまま保持すること。
@app.route('/shelter_register', methods=['GET', 'POST'])
@login_required
def shelter_register():
    if request.method == 'POST':
        errors = validate_registration(request.form)
        try:
            image_url = save_uploaded_image(request.files.get('image'))
        except ValueError as error:
            errors.append(str(error))
            image_url = None
        if errors:
            return render_template('shelter_register.html', **register_form_context(request.form, errors))
        session['shelter_register_draft'] = registration_payload(request.form, image_url)
        return redirect(url_for('shelter_register_confirm'))

    form = session.get('shelter_register_draft', {})
    return render_template('shelter_register.html', **register_form_context(form, success=request.args.get('success')))

@app.route('/shelter_register/confirm', methods=['GET', 'POST'])
@login_required
def shelter_register_confirm():
    draft = session.get('shelter_register_draft')
    if not draft:
        return redirect(url_for('shelter_register'))
    if request.method == 'POST':
        if request.form.get('action') == 'edit':
            return redirect(url_for('shelter_register'))
        next_id = max((shelter.get('id', 0) for shelter in shelters), default=0) + 1
        shelters.append({'id': next_id, **draft})
        save_shelters()
        session.pop('shelter_register_draft', None)
        return redirect(url_for('shelter_register', success='1'))
    return render_template('shelter_register_confirm.html', shelter=draft)

@app.route('/shelter_delete', methods=['POST'])
@login_required
def shelter_delete():
    try:
        shelter_id = int(request.form.get('delete_id', ''))
    except ValueError:
        return redirect(url_for('shelter_register'))
    shelters[:] = [shelter for shelter in shelters if shelter.get('id') != shelter_id]
    save_shelters()
    return redirect(url_for('shelter_register'))

@app.route('/api/save_draft', methods=['POST'])
@login_required
def save_draft():
    draft = request.get_json(silent=True) or request.form.to_dict(flat=False)
    session['shelter_register_draft'] = draft
    session.modified = True
    return jsonify({'ok': True})

# 避難所検索ページ
@app.route('/shelter_search')
def shelter_search():
    districts = sorted({normalize_shelter(s).get('district') or normalize_shelter(s).get('area')
                        for s in shelters if normalize_shelter(s).get('district') or normalize_shelter(s).get('area')})
    if not districts:
        districts = [AREA_NAME]
    return render_template(
        'shelter_search.html',
        name=request.args.get('name', ''),
        area=request.args.get('area', ''),
        selected_district=request.args.get('district', ''),
        districts=districts,
        map_shelters=[normalize_shelter(s) for s in shelters],
    )

# 全施設一覧ページ
@app.route('/all_shelters')
def all_shelters():
    return redirect(url_for('search_results'))


# 指示ボード：住民向けの指示を一覧で確認する
@app.route('/board')
@login_required
def board():
    resident_instructions = [i for i in instructions if i.get('target') == '住民']
    return render_template('board.html', instructions=resident_instructions)

# 検索結果ページ：templates/search_results.html を返す
@app.route('/search_results')
def search_results():
    name = request.args.get('name', '').strip()
    area = request.args.get('area', '').strip()
    district = request.args.get('district', '').strip()
    filters = {key for key in ('available', 'barrier_free', 'pets', 'infant', 'parking') if request.args.get(key) == 'on'}
    results = [s for s in filter_shelters(district, filters) if district
               if (not name or name in s.get('name', ''))
               and (not area or area in s.get('area', s.get('district', '')))]
    return render_template(
        'search_results.html',
        results=[enrich_shelter(s) for s in results],
        name=name,
        area=area,
        district=district,
        filters=filters,
    )

@app.route('/crowd_reports', methods=['POST'])
def add_crowd_report():
    status = request.form.get('status', '')
    redirect_params = {
        'name': request.form.get('name', ''),
        'area': request.form.get('area', ''),
        'district': request.form.get('district', ''),
    }
    for filter_name in ('available', 'barrier_free', 'pets', 'infant', 'parking'):
        if request.form.get(filter_name) == 'on':
            redirect_params[filter_name] = 'on'
    try:
        shelter_id = int(request.form.get('shelter_id', ''))
    except ValueError:
        return redirect(url_for('search_results', **redirect_params))
    if status in CROWD_OPTIONS and any(s.get('id') == shelter_id for s in shelters):
        crowd_reports.append({'shelter_id': shelter_id, 'status': status, 'posted_at': datetime.now(JST).isoformat()})
        save_crowd_reports()
    return redirect(url_for('search_results', **redirect_params))

# JSON API：/shelters?district=地区名
@app.route('/shelters', methods=['GET'])
def get_shelters():
    results = [enrich_shelter(s) for s in filter_shelters(request.args.get('district'))]
    if not results:
        return jsonify({'error': 'No shelters found'}), 404
    return jsonify(results)

# 気象警報・注意報API
@app.route('/api/weather_warnings')
def api_weather_warnings():
    return jsonify(get_weather_warnings())

if __name__ == '__main__':
    app.run(debug=True, port=5000)
