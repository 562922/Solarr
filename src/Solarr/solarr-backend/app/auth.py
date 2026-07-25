from functools import wraps
from flask import session, jsonify, request
from werkzeug.security import generate_password_hash, check_password_hash
import db


def create_user(username, password, role="user", email="", auto_approve=0):
    return db.run(
        "INSERT INTO users(username, password_hash, role, email, auto_approve) VALUES(?,?,?,?,?)",
        (username, generate_password_hash(password), role, email, int(auto_approve)),
    )


def verify_user(username, password):
    row = db.one("SELECT password_hash FROM users WHERE username=?", (username,))
    return bool(row) and check_password_hash(row["password_hash"], password)


def get_user(username):
    return db.one("SELECT id, username, role, email, auto_approve FROM users WHERE username=?", (username,))


def current_role():
    u = session.get("user")
    row = db.one("SELECT role FROM users WHERE username=?", (u,)) if u else None
    return row["role"] if row else None


def any_user_exists():
    return db.one("SELECT COUNT(*) c FROM users")["c"] > 0


def login_required(view):
    """No-op in single-user mode — all endpoints are always accessible."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """No-op in single-user mode — all endpoints are always accessible."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        return view(*args, **kwargs)
    return wrapped
