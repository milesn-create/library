import os
import hashlib
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, abort, send_from_directory)
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
import mysql.connector
import bleach
import markdown as md_lib
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')

UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = ('Для выполнения данного действия необходимо '
                                'пройти процедуру аутентификации')
login_manager.login_message_category = 'warning'

RATING_LABELS = {5: 'Отлично', 4: 'Хорошо', 3: 'Удовлетворительно',
                 2: 'Неудовлетворительно', 1: 'Плохо', 0: 'Ужасно'}

ALLOWED_TAGS = list(bleach.sanitizer.ALLOWED_TAGS) + [
    'p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'blockquote', 'pre', 'code', 'ul', 'ol', 'li', 'strong', 'em', 'hr'
]


# ── DB ─────────────────────────────────────────────────────────────────────────

def get_db():
    return mysql.connector.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        user=os.environ.get('DB_USER', 'root'),
        password=os.environ.get('DB_PASSWORD', ''),
        database=os.environ.get('DB_NAME', 'library'),
        charset='utf8mb4'
    )


# ── User model ─────────────────────────────────────────────────────────────────

class User(UserMixin):
    def __init__(self, row):
        self.id = row['id']
        self.login = row['login']
        self.last_name = row['last_name']
        self.first_name = row['first_name']
        self.middle_name = row.get('middle_name')
        self.role_id = row['role_id']
        self.role_name = row['role_name']

    @property
    def full_name(self):
        parts = [self.last_name, self.first_name]
        if self.middle_name:
            parts.append(self.middle_name)
        return ' '.join(parts)

    @property
    def is_admin(self):
        return self.role_name == 'Администратор'

    @property
    def is_moderator(self):
        return self.role_name in ('Администратор', 'Модератор')


@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        'SELECT u.*, r.name as role_name FROM users u '
        'JOIN roles r ON u.role_id=r.id WHERE u.id=%s', (user_id,))
    row = cur.fetchone()
    cur.close(); db.close()
    return User(row) if row else None


# ── Decorators ─────────────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Для выполнения данного действия необходимо пройти процедуру аутентификации', 'warning')
            return redirect(url_for('login'))
        if not current_user.is_admin:
            flash('У вас недостаточно прав для выполнения данного действия', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def moderator_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Для выполнения данного действия необходимо пройти процедуру аутентификации', 'warning')
            return redirect(url_for('login'))
        if not current_user.is_moderator:
            flash('У вас недостаточно прав для выполнения данного действия', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


# ── Helpers ────────────────────────────────────────────────────────────────────

def sanitize(text):
    return bleach.clean(text, tags=ALLOWED_TAGS, strip=True)


def md_to_html(text):
    return sanitize(md_lib.markdown(text or '', extensions=['fenced_code', 'tables']))


def compute_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


app.jinja_env.globals['RATING_LABELS'] = RATING_LABELS
app.jinja_env.filters['md'] = md_to_html


# ── Auth ───────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        login_val = request.form['login']
        password = request.form['password']
        remember = 'remember' in request.form
        db = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute(
            'SELECT u.*, r.name as role_name FROM users u '
            'JOIN roles r ON u.role_id=r.id WHERE u.login=%s', (login_val,))
        row = cur.fetchone()
        cur.close(); db.close()
        if row and check_password_hash(row['password_hash'], password):
            login_user(User(row), remember=remember)
            next_page = request.args.get('next') or url_for('index')
            return redirect(next_page)
        flash('Невозможно аутентифицироваться с указанными логином и паролем', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


# ── Index ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    page = request.args.get('page', 1, type=int)
    per_page = 10
    offset = (page - 1) * per_page
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT COUNT(*) as cnt FROM books')
    total = cur.fetchone()['cnt']
    cur.execute('''
        SELECT b.*, ROUND(AVG(r.rating),1) as avg_rating,
               COUNT(r.id) as review_count
        FROM books b LEFT JOIN reviews r ON r.book_id=b.id
        GROUP BY b.id ORDER BY b.year DESC LIMIT %s OFFSET %s
    ''', (per_page, offset))
    books = cur.fetchall()
    for book in books:
        cur.execute('''SELECT g.name FROM genres g JOIN book_genres bg ON bg.genre_id=g.id
                       WHERE bg.book_id=%s''', (book['id'],))
        book['genres'] = [row['name'] for row in cur.fetchall()]
        cur.execute('SELECT filename FROM covers WHERE book_id=%s LIMIT 1', (book['id'],))
        cov = cur.fetchone()
        book['cover'] = cov['filename'] if cov else None
    cur.close(); db.close()
    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template('index.html', books=books, page=page, total_pages=total_pages)


# ── Books ──────────────────────────────────────────────────────────────────────

@app.route('/books/<int:book_id>')
def book_detail(book_id):
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT * FROM books WHERE id=%s', (book_id,))
    book = cur.fetchone()
    if not book:
        abort(404)
    cur.execute('SELECT * FROM covers WHERE book_id=%s LIMIT 1', (book_id,))
    cover = cur.fetchone()
    cur.execute('''SELECT g.name FROM genres g JOIN book_genres bg ON bg.genre_id=g.id
                   WHERE bg.book_id=%s''', (book_id,))
    genres = [r['name'] for r in cur.fetchall()]
    cur.execute('''SELECT r.*, u.last_name, u.first_name, u.middle_name
                   FROM reviews r JOIN users u ON u.id=r.user_id
                   WHERE r.book_id=%s ORDER BY r.created_at DESC''', (book_id,))
    reviews = cur.fetchall()
    user_review = None
    user_collections = []
    if current_user.is_authenticated:
        cur.execute('SELECT * FROM reviews WHERE book_id=%s AND user_id=%s',
                    (book_id, current_user.id))
        user_review = cur.fetchone()
        cur.execute('SELECT id, name FROM collections WHERE user_id=%s ORDER BY name',
                    (current_user.id,))
        user_collections = cur.fetchall()
    cur.execute('SELECT ROUND(AVG(rating),1) as avg FROM reviews WHERE book_id=%s', (book_id,))
    avg_row = cur.fetchone()
    avg_rating = avg_row['avg'] if avg_row else None
    cur.close(); db.close()
    return render_template('book_detail.html', book=book, cover=cover,
                           genres=genres, reviews=reviews,
                           user_review=user_review, avg_rating=avg_rating,
                           user_collections=user_collections)


@app.route('/books/add', methods=['GET', 'POST'])
@admin_required
def book_add():
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT * FROM genres ORDER BY name')
    genres = cur.fetchall()
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = md_to_html(request.form.get('description', ''))
        year = request.form.get('year', '')
        publisher = request.form.get('publisher', '').strip()
        author = request.form.get('author', '').strip()
        pages = request.form.get('pages', '')
        genre_ids = request.form.getlist('genres')
        cover_file = request.files.get('cover')

        if not all([title, description, year, publisher, author, pages, genre_ids]) or \
           not cover_file or cover_file.filename == '':
            flash('При сохранении данных возникла ошибка. Проверьте корректность введённых данных.', 'danger')
            cur.close(); db.close()
            return render_template('book_form.html', genres=genres,
                                   book=request.form, selected_genres=genre_ids, is_edit=False)
        cover_data = cover_file.read()
        md5 = compute_md5(cover_data)
        mime = cover_file.mimetype
        try:
            cur.execute('''INSERT INTO books (title, description, year, publisher, author, pages)
                           VALUES (%s,%s,%s,%s,%s,%s)''',
                        (title, description, year, publisher, author, pages))
            book_id = cur.lastrowid
            for gid in genre_ids:
                cur.execute('INSERT INTO book_genres (book_id, genre_id) VALUES (%s,%s)',
                            (book_id, gid))
            # Обложка — проверяем дубликат по MD5
            cur.execute('SELECT id, filename FROM covers WHERE md5_hash=%s LIMIT 1', (md5,))
            existing = cur.fetchone()
            if existing and os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], existing['filename'])):
                cur.execute('INSERT INTO covers (filename, mime_type, md5_hash, book_id) VALUES (%s,%s,%s,%s)',
                            (existing['filename'], mime, md5, book_id))
            else:
                cur.execute('INSERT INTO covers (filename, mime_type, md5_hash, book_id) VALUES (%s,%s,%s,%s)',
                            ('tmp', mime, md5, book_id))
                cover_id = cur.lastrowid
                ext = (cover_file.filename.rsplit('.', 1)[-1].lower()
                       if '.' in cover_file.filename else 'jpg')
                filename = f'{cover_id}.{ext}'
                cur.execute('UPDATE covers SET filename=%s WHERE id=%s', (filename, cover_id))
                with open(os.path.join(app.config['UPLOAD_FOLDER'], filename), 'wb') as f:
                    f.write(cover_data)
            db.commit()
            cur.close(); db.close()
            flash('Книга успешно добавлена', 'success')
            return redirect(url_for('book_detail', book_id=book_id))
        except Exception as e:
            db.rollback()
            cur.close(); db.close()
            flash('При сохранении данных возникла ошибка. Проверьте корректность введённых данных.', 'danger')
            return render_template('book_form.html', genres=genres,
                                   book=request.form, selected_genres=genre_ids, is_edit=False)
    cur.close(); db.close()
    return render_template('book_form.html', genres=genres, book={}, selected_genres=[], is_edit=False)


@app.route('/books/<int:book_id>/edit', methods=['GET', 'POST'])
@moderator_required
def book_edit(book_id):
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT * FROM books WHERE id=%s', (book_id,))
    book = cur.fetchone()
    if not book:
        abort(404)
    cur.execute('SELECT * FROM genres ORDER BY name')
    genres = cur.fetchall()
    cur.execute('SELECT genre_id FROM book_genres WHERE book_id=%s', (book_id,))
    selected_genres = [str(r['genre_id']) for r in cur.fetchall()]
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = md_to_html(request.form.get('description', ''))
        year = request.form.get('year', '')
        publisher = request.form.get('publisher', '').strip()
        author = request.form.get('author', '').strip()
        pages = request.form.get('pages', '')
        genre_ids = request.form.getlist('genres')
        try:
            cur.execute('''UPDATE books SET title=%s, description=%s, year=%s,
                           publisher=%s, author=%s, pages=%s WHERE id=%s''',
                        (title, description, year, publisher, author, pages, book_id))
            cur.execute('DELETE FROM book_genres WHERE book_id=%s', (book_id,))
            for gid in genre_ids:
                cur.execute('INSERT INTO book_genres (book_id, genre_id) VALUES (%s,%s)',
                            (book_id, gid))
            db.commit()
            cur.close(); db.close()
            flash('Книга успешно обновлена', 'success')
            return redirect(url_for('book_detail', book_id=book_id))
        except Exception:
            db.rollback()
            cur.close(); db.close()
            flash('При сохранении данных возникла ошибка. Проверьте корректность введённых данных.', 'danger')
            return render_template('book_form.html', genres=genres, book=request.form,
                                   selected_genres=genre_ids, is_edit=True, book_id=book_id)
    cur.close(); db.close()
    return render_template('book_form.html', genres=genres, book=book,
                           selected_genres=selected_genres, is_edit=True, book_id=book_id)


@app.route('/books/<int:book_id>/delete', methods=['POST'])
@admin_required
def book_delete(book_id):
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT filename FROM covers WHERE book_id=%s', (book_id,))
    covers = cur.fetchall()
    try:
        for c in covers:
            cur.execute('SELECT COUNT(*) as cnt FROM covers WHERE filename=%s AND book_id != %s', (c['filename'], book_id))
            if cur.fetchone()['cnt'] == 0:
                path = os.path.join(app.config['UPLOAD_FOLDER'], c['filename'])
                if os.path.exists(path):
                    os.remove(path)
        cur.execute('DELETE FROM books WHERE id=%s', (book_id,))
        db.commit()
        flash('Книга успешно удалена', 'success')
    except Exception:
        db.rollback()
        flash('Ошибка при удалении книги', 'danger')
    cur.close(); db.close()
    return redirect(url_for('index'))


# ── Reviews ────────────────────────────────────────────────────────────────────

@app.route('/books/<int:book_id>/review', methods=['GET', 'POST'])
@login_required
def review_add(book_id):
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT * FROM books WHERE id=%s', (book_id,))
    book = cur.fetchone()
    if not book:
        abort(404)
    cur.execute('SELECT id FROM reviews WHERE book_id=%s AND user_id=%s',
                (book_id, current_user.id))
    if cur.fetchone():
        cur.close(); db.close()
        flash('Вы уже оставляли рецензию на эту книгу', 'warning')
        return redirect(url_for('book_detail', book_id=book_id))
    if request.method == 'POST':
        rating = request.form.get('rating')
        text = sanitize(request.form.get('text', ''))
        try:
            cur.execute('INSERT INTO reviews (book_id, user_id, rating, text) VALUES (%s,%s,%s,%s)',
                        (book_id, current_user.id, rating, text))
            db.commit()
            cur.close(); db.close()
            flash('Рецензия успешно добавлена', 'success')
            return redirect(url_for('book_detail', book_id=book_id))
        except Exception:
            db.rollback()
            cur.close(); db.close()
            flash('При сохранении данных возникла ошибка. Проверьте корректность введённых данных.', 'danger')
            return render_template('review_form.html', book=book, form=request.form)
    cur.close(); db.close()
    return render_template('review_form.html', book=book, form={})


# ── Collections (Вариант 2) ────────────────────────────────────────────────────

@app.route('/collections')
@login_required
def my_collections():
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute('''
        SELECT c.id, c.name, COUNT(cb.book_id) as book_count
        FROM collections c
        LEFT JOIN collection_books cb ON cb.collection_id=c.id
        WHERE c.user_id=%s GROUP BY c.id ORDER BY c.name
    ''', (current_user.id,))
    collections = cur.fetchall()
    cur.close(); db.close()
    return render_template('collections.html', collections=collections)


@app.route('/collections/add', methods=['POST'])
@login_required
def collection_add():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Название подборки не может быть пустым', 'danger')
        return redirect(url_for('my_collections'))
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute('INSERT INTO collections (name, user_id) VALUES (%s,%s)',
                    (name, current_user.id))
        db.commit()
        flash('Подборка успешно создана', 'success')
    except Exception:
        db.rollback()
        flash('Ошибка при создании подборки', 'danger')
    cur.close(); db.close()
    return redirect(url_for('my_collections'))


@app.route('/collections/<int:col_id>')
@login_required
def collection_detail(col_id):
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT * FROM collections WHERE id=%s AND user_id=%s',
                (col_id, current_user.id))
    col = cur.fetchone()
    if not col:
        abort(404)
    cur.execute('''
        SELECT b.*, ANY_VALUE(cv.filename) as cover,
               ROUND(AVG(r.rating),1) as avg_rating, COUNT(r.id) as review_count
        FROM books b
        JOIN collection_books cb ON cb.book_id=b.id
        LEFT JOIN covers cv ON cv.book_id=b.id
        LEFT JOIN reviews r ON r.book_id=b.id
        WHERE cb.collection_id=%s GROUP BY b.id ORDER BY b.title
    ''', (col_id,))
    books = cur.fetchall()
    cur.close(); db.close()
    return render_template('collection_detail.html', collection=col, books=books)


@app.route('/collections/add_book', methods=['POST'])
@login_required
def collection_add_book():
    book_id = request.form.get('book_id', type=int)
    collection_id = request.form.get('collection_id', type=int)
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT id FROM collections WHERE id=%s AND user_id=%s',
                (collection_id, current_user.id))
    if not cur.fetchone():
        cur.close(); db.close()
        flash('Подборка не найдена', 'danger')
        return redirect(url_for('book_detail', book_id=book_id))
    try:
        cur.execute('INSERT IGNORE INTO collection_books (collection_id, book_id) VALUES (%s,%s)',
                    (collection_id, book_id))
        db.commit()
        flash('Книга успешно добавлена в подборку', 'success')
    except Exception:
        db.rollback()
        flash('Ошибка при добавлении книги в подборку', 'danger')
    cur.close(); db.close()
    return redirect(url_for('book_detail', book_id=book_id))


# ── Misc ───────────────────────────────────────────────────────────────────────

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.cli.command('create-admin')
def create_admin():
    """Create admin user interactively."""
    import click
    login_val = click.prompt('Login')
    password = click.prompt('Password', hide_input=True)
    last_name = click.prompt('Last name')
    first_name = click.prompt('First name')
    db = get_db()
    cur = db.cursor()
    cur.execute(
        'INSERT INTO users (login, password_hash, last_name, first_name, role_id) VALUES (%s,%s,%s,%s,1)',
        (login_val, generate_password_hash(password), last_name, first_name)
    )
    db.commit()
    cur.close(); db.close()
    click.echo('Admin created successfully.')


if __name__ == '__main__':
    app.run(debug=True)
