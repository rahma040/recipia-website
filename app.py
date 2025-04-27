
import os
print(f"Absolute path to dashboard.html: {os.path.abspath('templates/dashboard.html')}")
import sys
from flask import Flask, json, jsonify, render_template, request, redirect, url_for, session, flash
from dotenv import load_dotenv
import requests
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from sqlalchemy import Enum as SQLAlchemyEnum
from werkzeug.utils import secure_filename
import logging
from markupsafe import Markup
from functools import wraps
from datetime import datetime, timedelta, timezone
import mimetypes
app = Flask(__name__)

@app.route("/template_path")
def template_path():
    import os
    full_path = os.path.join(app.template_folder, 'dashboard.html')
    return {
        "template_folder": app.template_folder,
        "full_path": full_path,
        "exists": os.path.exists(full_path),
        "content": open(full_path).read(200) + "..." if os.path.exists(full_path) else "N/A"
    }

# --- Gemini Import ---
try:
    from gemini_utils import generate_recipe_ideas, get_or_generate_detailed_recipe, get_detail_query_hash
    GEMINI_ENABLED = True
    logging.info("Successfully imported Gemini utilities.")
except ImportError as e:
    logging.warning(f"gemini_utils.py not found... Gemini disabled. Error: {e}")
    GEMINI_ENABLED = False
    def generate_recipe_ideas(*args, **kwargs): return {"error": "Gemini feature (ideas) unavailable."}
    def get_or_generate_detailed_recipe(*args, **kwargs): return {"error": "Gemini feature (details) unavailable."}
    def get_detail_query_hash(*args, **kwargs): return None



# --- Configuration ---
load_dotenv()
spoonacular_api_key = os.getenv("SPOONACULAR_API_KEY")
db_path = os.path.join(app.instance_path, 'dev.db')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', f'sqlite:///{db_path}')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", os.urandom(24))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
ALLOWED_MIMETYPES = {'image/png', 'image/jpeg', 'image/gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024
if not os.path.exists(app.instance_path): os.makedirs(app.instance_path)
if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)

# --- Initialize SQLAlchemy ---
db = SQLAlchemy(app)

# --- Jinja Filter ---
def nl2br(value):
    if value is None: return ''
    return Markup(str(value).replace('\n', '<br>\n'))
app.jinja_env.filters['nl2br'] = nl2br

# --- Helper Functions ---
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

CACHE_FILE = "image_cache.json"

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logging.error(f"Error decoding JSON from {CACHE_FILE}")
            return {}
        except IOError as e:
            logging.error(f"Error reading cache file {CACHE_FILE}: {e}")
            return {}
    return {}

# ========================
# DATABASE MODELS
# ========================

# Define enum type first
RecipeTypeEnum = SQLAlchemyEnum('spoonacular', 'gemini', name='recipe_type_enum')

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    first_name = db.Column(db.String(50), nullable=True)
    last_name = db.Column(db.String(50), nullable=True)
    profile_picture_url = db.Column(db.String(255), nullable=True)
    dietary_preferences = db.Column(db.Text, nullable=True)
    allergies = db.Column(db.Text, nullable=True)
    bio = db.Column(db.Text, nullable=True)
    favorites = db.relationship('FavoriteRecipe', backref='user', lazy=True, cascade="all, delete-orphan")
    playlists = db.relationship('Playlist', backref='user', lazy=True, cascade="all, delete-orphan")
    search_history = db.relationship('SearchHistory', backref='user', lazy=True, cascade="all, delete-orphan")

class FavoriteRecipe(db.Model):
    __tablename__ = 'favorite_recipes'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    recipe_type = db.Column(RecipeTypeEnum, nullable=False)
    recipe_identifier = db.Column(db.String(70), nullable=False)
    recipe_name = db.Column(db.String(255), nullable=False)
    favorited_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())
    __table_args__ = (db.UniqueConstraint('user_id', 'recipe_type', 'recipe_identifier', name='uq_user_recipe'),)

class Playlist(db.Model):
    __tablename__ = 'playlists'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())
    items = db.relationship('PlaylistItem', backref='playlist', lazy=True, cascade="all, delete-orphan")

class PlaylistItem(db.Model):
    __tablename__ = 'playlist_items'
    id = db.Column(db.Integer, primary_key=True)
    playlist_id = db.Column(db.Integer, db.ForeignKey('playlists.id', ondelete='CASCADE'), nullable=False)
    recipe_identifier = db.Column(db.String(255), nullable=False)
    recipe_type = db.Column(RecipeTypeEnum, nullable=False)
    recipe_name = db.Column(db.String(255), nullable=False)
    added_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())

class SearchHistory(db.Model):
    __tablename__ = 'search_history'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    ingredients = db.Column(db.String(255), nullable=False)
    search_date = db.Column(db.DateTime(timezone=True), server_default=db.func.now())
    strict_mode = db.Column(db.Boolean, default=False)

# Create tables
with app.app_context():
    db.create_all()

# ========================
# AUTHENTICATION & USER MANAGEMENT
# ========================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'loggedin' not in session:
            flash('Please log in to access this page.', 'warning')
            session['next_url'] = request.url
            return redirect(url_for('login'))
        session.pop('next_url', None)
        return f(*args, **kwargs)
    return decorated_function

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get('loggedin'):
        return redirect(url_for('dashboard'))

    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")
        
        try:
            user = db.session.execute(db.select(User).where(User.email == email)).scalar_one_or_none()
            if user and user.password == password:  # In production, use password hashing
                session.permanent = True
                session['loggedin'] = True
                session['user_id'] = user.id
                session['username'] = user.username
                session['email'] = user.email
                session['first_name'] = user.first_name
                session['last_name'] = user.last_name
                session['profile_picture_url'] = user.profile_picture_url
                session['dietary_preferences'] = user.dietary_preferences
                session['allergies'] = user.allergies
                session['bio'] = user.bio
                flash("Login successful!", "success")
                return redirect(url_for("dashboard"))
            else:
                flash("Invalid email or password.", "error")
        except Exception as e:
            logging.error(f"Login error: {e}")
            flash("Login error.", "error")

    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get('loggedin'):
        return redirect(url_for('dashboard'))

    if request.method == "POST":
        username = request.form.get("username")
        email = request.form.get("email")
        password = request.form.get("password")
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        dietary_preferences = request.form.get("dietary_preferences", "").strip()
        allergies = request.form.get("allergies", "").strip()
        bio = request.form.get("bio", "").strip()

        try:
            existing_user = db.session.execute(
                db.select(User).where((User.username == username) | (User.email == email))
            ).scalar_one_or_none()
            
            if existing_user:
                flash("Username or Email exists.", "error")
            else:
                new_user = User(
                    username=username,
                    email=email,
                    password=password,  # In production, hash this
                    first_name=first_name,
                    last_name=last_name,
                    dietary_preferences=dietary_preferences,
                    allergies=allergies,
                    bio=bio
                )
                db.session.add(new_user)
                db.session.commit()
                flash("Registration successful! Please log in.", "success")
                return redirect(url_for("login"))
        except Exception as e:
            db.session.rollback()
            logging.error(f"Registration error: {e}")
            flash("Registration failed.", "error")

    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))

# ========================
# RECIPE & SEARCH HISTORY FUNCTIONALITY
# ========================

@app.route("/")
def index():
    if session.get('loggedin'):
        return redirect(url_for('dashboard'))
    return render_template("index.html")

@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session['user_id']
    try:
        favorites = db.session.execute(
            db.select(FavoriteRecipe)
            .where(FavoriteRecipe.user_id == user_id)
            .order_by(FavoriteRecipe.favorited_at.desc())
        ).scalars().all()
        
        playlists = db.session.execute(
            db.select(Playlist)
            .where(Playlist.user_id == user_id)
            .order_by(Playlist.created_at.desc())
        ).scalars().all()
        
        playlist_items = {}
        for playlist in playlists:
            items = db.session.execute(
                db.select(PlaylistItem)
                .where(PlaylistItem.playlist_id == playlist.id)
                .order_by(PlaylistItem.added_at.desc())
            ).scalars().all()
            playlist_items[playlist.id] = items
        
        # Get recent search history (last 5 searches)
        search_history = db.session.execute(
            db.select(SearchHistory)
            .where(SearchHistory.user_id == user_id)
            .order_by(SearchHistory.search_date.desc())
            .limit(5)
        ).scalars().all()
        
        return render_template(
            "dashboard.html", 
            favorites=favorites, 
            playlists=playlists,
            playlist_items=playlist_items,
            search_history=search_history
        )
    except Exception as e:
        logging.error(f"Error loading dashboard: {e}")
        flash("Could not load dashboard data.", "error")
        return redirect(url_for('index'))

@app.route("/search_history")
@login_required
def view_search_history():
    user_id = session['user_id']
    try:
        search_history = db.session.execute(
            db.select(SearchHistory)
            .where(SearchHistory.user_id == user_id)
            .order_by(SearchHistory.search_date.desc())
        ).scalars().all()
        
        return render_template("search_history.html", search_history=search_history)
    except Exception as e:
        logging.error(f"Error loading search history: {e}")
        flash("Could not load search history.", "error")
        return redirect(url_for('dashboard'))

@app.route("/process")
def process():
    ingredients = request.args.get("ingredients", "").strip()
    strict_mode = 'strict_mode' in request.args
    
    if not ingredients:
        flash("No ingredients provided.", "warning")
        return redirect(url_for('dashboard' if session.get('loggedin') else 'index'))
    
    # Save search to history if user is logged in
    if session.get('loggedin'):
        try:
            new_search = SearchHistory(
                user_id=session['user_id'],
                ingredients=ingredients,
                strict_mode=strict_mode
            )
            db.session.add(new_search)
            db.session.commit()
        except Exception as e:
            logging.error(f"Error saving search history: {e}")
    
    wait_time = 1800
    redirect_params = {'ingredients': ingredients}
    if strict_mode:
        redirect_params['strict_mode'] = 'true'
    
    return render_template(
        "processing.html",
        wait_time=wait_time,
        redirect_url=url_for("suggestions", **redirect_params)
    )

@app.route("/suggestions")
def suggestions():
    ingredients = request.args.get("ingredients", "").strip()
    strict_mode = 'strict_mode' in request.args
    
    if not ingredients:
        flash("No ingredients provided.", "warning")
        return redirect(url_for('dashboard' if session.get('loggedin') else 'index'))

    if session.get('loggedin') and GEMINI_ENABLED:
        user_id = session.get('user_id', 'Unknown')
        diet = session.get('dietary_preferences', '')
        allergies = session.get('allergies', '')
        exclude = ''
        
        try:
            ideas_result = generate_recipe_ideas(ingredients, diet, exclude, allergies, strict_mode)
            
            if 'error' in ideas_result:
                flash(f"Error: {ideas_result['error']}", "error")
                ideas_data = ideas_result
            else:
                ideas_data = ideas_result.get('ideas', [])
                if ideas_data:
                    flash(f"Found {len(ideas_data)} AI ideas!", "info")
                else:
                    flash("AI couldn't find ideas.", "warning")
            
            original_query = {
                'ingredients': ingredients,
                'diet': diet or '',
                'allergies': allergies or '',
                'exclude': exclude or '',
                'strict_mode': strict_mode
            }
            
            return render_template(
                "gemini_list.html",
                ideas=ideas_data,
                error=None,
                original_query=original_query
            )
            
        except Exception as e:
            flash("Server error getting ideas.", "error")
            logging.error(f"Error: {e}", exc_info=True)
            return render_template(
                "gemini_list.html",
                ideas=[],
                error="Server error",
                original_query={}
            )
    else:
        recipes_data = get_recipes_from_spoonacular(ingredients)
        return render_template(
            "suggestions.html",
            recipes=recipes_data,
            ingredients=ingredients,
            source='spoonacular'
        )

# ... [Rest of your existing routes remain unchanged] ...

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )
    
    if not os.getenv("SECRET_KEY"):
        logging.warning("SECRET_KEY environment variable not set. Using temporary key.")
    
    if not os.getenv("SPOONACULAR_API_KEY"):
        logging.warning("SPOONACULAR_API_KEY not set. Spoonacular features will be unavailable.")
    
    app.run(debug=True, port=5000)