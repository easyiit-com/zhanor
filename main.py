import json
import os
from os.path import isdir
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import re
import importlib
from flask import (
    Blueprint,
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
    current_app,
)
from flask_babel import Babel
from flask_compress import Compress
from flask_jwt_extended import JWTManager
from flask_jwt_extended.exceptions import JWTExtendedException
from flask_restful import Api
from flask_wtf.csrf import CSRFError
from sqlalchemy import inspect
from app.api import load_apis
from app.core import db
from app.core.base_response import Response
from app.core.process_rules import organize_admin_rules, organize_user_rules
from app.models.admin import Admin
from app.models.admin_log import AdminLog
from app.models.admin_rule import AdminRule
from app.models.general_config import GeneralConfig
from app.core.base import Base
from app.models.user import User
from app.models.user_rule import UserRule
from app.utils import languages
from app.utils.logger import logger
from app.core.csrf import csrf
from app.core.user.login import current_user
from app.core.admin.login import current_admin
from app.core.admin.auth import admin_login_manager
from app.core.user.auth import login_manager
from config import Config


def get_version():
    """获取版本号"""
    version_file = os.path.join(os.path.dirname(__file__), "VERSION")
    with open(version_file) as f:
        return f.read().strip()


__version__ = get_version()


def get_locale():
    """获取用户的语言偏好"""
    return request.accept_languages.best_match(["en", "zh"])


def create_app(test_config=None):
    """创建并配置Flask应用实例。"""
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder="app/templates",
        static_folder="app/static",
    )

    app.config.from_object(Config)

    # 初始化Babel和CSRF
    babel = Babel(app, locale_selector=get_locale)
    csrf.init_app(app)

    # 管理员和用户登录管理
    admin_login_manager.init_app(app)
    login_manager.init_app(app)

    @admin_login_manager.admin_loader
    def load_admin(user_id):
        return Admin.query.get(int(user_id))

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    @app.before_request
    def before_request():
        """处理请求前的操作"""
        g.user = (
            current_user._get_current_object()
            if current_user.is_authenticated
            else None
        )
        g.admin = (
            current_admin._get_current_object()
            if current_admin and current_admin.is_authenticated
            else None
        )

        if request.method == "POST":
            log_request_data()

    def log_request_data():
        """记录请求数据"""
        try:
            user_agent = request.headers.get("User-Agent")
            route_name = request.endpoint
            full_url = request.url
            request_json = request.get_json(silent=True) or {}
            log_json = request_json.copy()
            if "password" in log_json:
                log_json["password"] = ""

            content_str = json.dumps(log_json)

            if g.user:
                admin_log = AdminLog(
                    admin_id=g.user.id,
                    username=g.user.name,
                    url=full_url,
                    title=route_name,
                    content=content_str,
                    ip=request.remote_addr,
                    useragent=user_agent,
                    createtime=datetime.utcnow(),
                )
                db.session.add(admin_log)
                db.session.commit()
                print(f"请求的JSON数据（已序列化）: {content_str}")
        except Exception as e:
            logger.error(f"处理请求时发生错误: {e}")

    @app.route("/static/<path:path>")
    def serve_static(path):
        """提供静态文件"""
        return send_from_directory(app.static_folder, path)

    @app.errorhandler(404)
    def page_not_found(e):
        """404错误处理"""
        logger.error("页面未找到: %s", request.path)
        return render_template("404.jinja2", e=e), 404

    @app.errorhandler(403)
    def forbidden(e):
        """403错误处理"""
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return Response.error(code=403, msg="未经授权的访问")
        else:
            return render_template("403.jinja2", e=e), 403

    @login_manager.unauthorized_handler
    def unauthorized():
        """处理未授权访问"""
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return Response.error(code=403, msg="未经授权的访问")
        return redirect(url_for("user_auth.login", next=request.url))

    @admin_login_manager.unauthorized_handler
    def unauthorized_admin():
        """处理管理员未授权访问"""
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return Response.error(code=403, msg="未经授权的访问")
        return redirect(url_for("admin_auth.login", next=request.url))

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        """处理CSRF错误"""
        return Response.error(msg="无效的CSRF令牌。", data=e.description)

    @app.errorhandler(JWTExtendedException)
    def handle_jwt_error(e):
        """处理JWT错误"""
        return jsonify(msg=str(e), time=int(datetime.utcnow().timestamp())), 401

    # 数据库初始化
    db.init_app(app)

    # 注册蓝图
    register_blueprints(app)
    # 插件
    plugin_admin_rules = []
    plugin_user_rules = []

    def scan_plugins_folder(plugin_dir: str):
        """
        导入插件API路由。
        """
        plugins_folder = Path(plugin_dir)

        for sub_dir in plugins_folder.glob("**/"):
            if not sub_dir.is_dir():
                continue
            router_file = sub_dir / "views.py"
            if router_file.is_file():
                module_name = f"app.plugins.{sub_dir.parts[-2]}.{sub_dir.parts[-1]}"
                try:
                    plugin_module = importlib.import_module(f"{module_name}.views")
                    router_instance = getattr(plugin_module, "bp", None)
                    if isinstance(router_instance, Blueprint):
                        app.register_blueprint(router_instance)
                    else:
                        logger.error(f"scan_plugins_folder====>未知:{module_name}")
                except Exception as e:
                    logger.error(
                        f"scan_plugins_folder====>导入插件:{module_name}, 捕获错误：{e}"
                    )

        plugin_json_file = plugins_folder / "plugin.json"
        if plugin_json_file.is_file():
            try:
                with open(plugin_json_file, "r") as file:
                    plugin_data = file.read()
                loaded_data = json.loads(plugin_data)
                admin_menu = loaded_data.get("admin_menu", [])
                for item in admin_menu:
                    admin_rule = AdminRule(
                        id=item["id"],
                        type=item["type"],
                        pid=item["pid"],
                        addon=1,  # 假设从插件来的 addon 值
                        name=item["name"],
                        url_path=item["url_path"],
                        title=item["title"],
                        description=item.get("description", ""),
                        icon=item.get("icon", ""),
                        menutype=item.get("menutype", ""),
                        extend=item.get("extend", ""),
                        model_name="plugin",  # 假设模型名称
                        createtime=datetime.strptime(
                            item["createtime"], "%Y-%m-%d %H:%M:%S"
                        ),
                        updatetime=datetime.strptime(
                            item["updatetime"], "%Y-%m-%d %H:%M:%S"
                        ),
                        weigh=item["weigh"],
                        status=item["status"],
                    )
                    plugin_admin_rules.append(admin_rule)
                user_rule = loaded_data.get("user_menu", [])
                plugin_user_rules.extend(user_rule)
            except Exception as e:
                logger.error(
                    f"scan_plugins_folder====>导入插件菜单:{module_name} 捕获错误：{e}"
                )

        create_plugin_models(plugins_folder)

    # 插件
    current_dir = os.getcwd()
    plugins_directory = os.path.join(current_dir, "app", "plugins")
    try:
        with open(os.path.join(plugins_directory, "plugins_status.json"), "r") as f:
            plugins_status = json.load(f)
    except FileNotFoundError:
        plugins_status = {}
    except json.JSONDecodeError:
        with open(os.path.join(plugins_directory, "plugins_status.json"), "w") as f:
            f.truncate(0)
        plugins_status = {}

    with app.app_context():  # 确保在应用上下文中调用
        for key, value in plugins_status.items():
            plugin_name = key
            plugin_dir = os.path.join(plugins_directory, plugin_name)
            if value == "enabled" and isdir(plugin_dir):
                scan_plugins_folder(plugin_dir)

    # 初始化JWT
    jwt = JWTManager(app)

    # 创建并配置API蓝图
    api_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")
    csrf.exempt(api_bp)
    api = Api(api_bp)
    load_apis.autoload_apis(app, api)
    app.register_blueprint(api_bp)

    @app.context_processor
    def inject_global_variables():
        """注入全局变量到模板中"""

        admin_rules = get_admin_rules()
        admin_rules.extend(plugin_admin_rules)

        user_rules = get_user_rules()
        # user_rules.extend(plugin_user_rules)
        organized_user_rules = organize_user_rules(user_rules)
        admin_rules_all = get_admin_rules_all()
        organized_admin_rules = organize_admin_rules(admin_rules)

        global_val = dict(
            title="zhanor",
            get_timestamp=get_timestamp(),
            configs=get_general_configs(),
            all_languages=languages,
            admin_rules=organized_admin_rules,
            admin_rules_all=admin_rules_all,
            user_rules=organized_user_rules,
            user_rules_query=get_user_rules_query(),
            user_rules_all=get_user_rules_all(),
            admin=g.admin,
            user=g.user,
            breadcrumbs=process_breadcrumbs(),
            current_route=str(request.url_rule),
            current_parent_path=re.sub(
                r"(/add|/edit/\d+)",
                "",
                str(request.url_rule),
            ),
        )
        return global_val

    # 设置主页视图
    app.add_url_rule(
        "/", endpoint="index", view_func=lambda: render_template("index.jinja2")
    )

    # 压缩
    Compress(app)
    return app


def register_blueprints(app):
    """注册蓝图"""
    views_base_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "app", "views")
    )
    for root, dirs, files in os.walk(views_base_path):
        relative_root = os.path.relpath(root, views_base_path)
        for file in files:
            if file.endswith(".py") and file != "__init__.py":
                module_path = (
                    "app.views."
                    + ("." + relative_root).lstrip(".")
                    + "."
                    + os.path.splitext(file)[0]
                )
                module = importlib.import_module(module_path)
                bp = getattr(module, "bp", None)
                if bp is not None:
                    app.register_blueprint(bp)


def create_plugin_models(plugin_dir):
    """创建插件的数据库模型"""
    models_dir = plugin_dir / "models"
    db_engine = db.get_db_engine()

    if models_dir.is_dir():
        for model_file in models_dir.glob("*.py"):
            try:
                model_module = importlib.import_module(
                    f"app.plugins.{plugin_dir.name}.models.{model_file.stem}"
                )

                # 遍历模型模块中的所有类，查找继承自 Base 的类
                for name, cls in model_module.__dict__.items():
                    if (
                        isinstance(cls, type)
                        and issubclass(cls, Base)
                        and cls is not Base
                    ):  # 确保是 db.Model 的子类
                        inspector = inspect(db_engine)
                        if not inspector.has_table(
                            cls.__tablename__
                        ):  # 使用 inspector 检查表存在性
                            cls.metadata.create_all(
                                db_engine
                            )  # 使用 cls 而不是 model_module.Base
            except Exception as e:
                logger.error(f"导入模型时出错: {model_file.stem}, 错误: {e}")


def get_timestamp():
    """获取当前时间戳"""
    import datetime

    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_general_configs():
    """获取所有通用配置"""
    general_configs = GeneralConfig.query.all()
    configs = defaultdict(dict)
    for item in general_configs:
        value_as_dict = (
            list(enumerate(json.loads(item.value)))
            if item.type == "array"
            else item.value
        )
        configs[item.group][item.name] = value_as_dict
    return configs


def get_admin_rules():
    """获取管理员规则。"""
    admin_rules = (
        AdminRule.query.filter_by(type="menu", status="normal")
        .order_by(AdminRule.id.asc())
        .all()
    )
    return admin_rules


def get_admin_rules_all():
    """获取全部管理员规则。"""
    admin_rules_all = AdminRule.query.filter(AdminRule.status == "normal").order_by(
        AdminRule.id.asc()
    )
    return admin_rules_all


def get_user_rules():
    """获取用户规则。"""
    user_rules = UserRule.query.filter_by(pid=0, type="menu", status="normal").order_by(UserRule.id.asc()).all()
    return user_rules


def get_user_rules_query():
    """获取用户规则_query。"""
    user_rules_query = (
        UserRule.query.filter(
            UserRule.pid == 0,
            UserRule.type == "menu",
            UserRule.status == "normal",
        )
        .order_by(UserRule.id.asc())
        .all()
    )
    return user_rules_query


def get_user_rules_all():
    """获取全部用户规则。"""
    user_rules_all = (
        UserRule.query.filter(
            UserRule.pid == 0,
            UserRule.type == "menu",
            UserRule.status == "normal",
        )
        .order_by(UserRule.id.asc())
        .all()
    )
    return user_rules_all


def process_breadcrumbs():
    """处理面包屑路径。"""
    s = str(request.url_rule).replace("/admin/", "", 1)
    parts = s.split("/")
    if not parts:
        return ""
    back_to_dashboard = '<a href="/admin/dashboard">返回仪表板</a>'
    title = parts[0].capitalize()
    if len(parts) == 1:
        return f'{back_to_dashboard}<a href="#">{title}</a>'
    elif len(parts) == 2:
        return f'{back_to_dashboard}<a href="/admin/{title}/{parts[1]}">{title} {parts[1].capitalize()}</a>'
    elif len(parts) == 3:
        return f'{back_to_dashboard}<a href="/admin/{title}">{title} {parts[1].capitalize()}</a> / {parts[2].capitalize()}'
    return ""


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), debug=True)
