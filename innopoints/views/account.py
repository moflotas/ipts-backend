"""Views related to the Account model.

Account:
- GET    /account
- GET    /accounts/{email}
- GET    /accounts
- GET    /accounts/groups
- PATCH  /accounts/{email}/balance
- GET    /account/timeline
- GET    /accounts/{email}/timeline
- GET    /account/statistics
- GET    /accounts/{email}/statistics
- GET    /account/notification_settings
- GET    /accounts/{email}/notification_settings
- POST   /accounts/{email}/notify
- PATCH  /account/telegram
- PATCH  /accounts/{email}/telegram
- PATCH  /account/notification_settings
- PATCH  /accounts/{email}/notification_settings

Innopoints:
- POST   /reclaim-innopoints
"""

from datetime import datetime
import logging
import sqlite3
import math

from flask import request, jsonify, session
from flask_login import login_required, current_user
from marshmallow import ValidationError
from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash

from innopoints.blueprints import api
from innopoints.core.helpers import abort, admin_required
from innopoints.core.timezone import tz_aware_now, unix_epoch
from innopoints.core.notifications import notify
from innopoints.extensions import db
from innopoints.models import (
    Account,
    Activity,
    Application,
    ApplicationStatus,
    Competence,
    Feedback,
    feedback_competence,
    LifetimeStage,
    Notification,
    NotificationType,
    Product,
    Project,
    StockChange,
    Transaction,
    Variety,
    VolunteeringReport,
)
from innopoints.schemas import AccountSchema, TimelineSchema, NotificationSettingsSchema

NO_PAYLOAD = ('', 204)
log = logging.getLogger(__name__)


def subquery_to_events(subquery, event_type):
    """Take a subquery that has an 'entry_time' field and output a query
    that packs the rest of the fields into a JSON payload and returns it with the time."""
    # payload = db.func.row_to_json(query).cast(JSONB) - 'entry_time'
    return db.session.query(
        subquery.c.entry_time.label('entry_time'),
        db.literal(event_type).label('type'),
        (db.func.row_to_json(db.literal_column(subquery.name)).cast(JSONB) - 'entry_time').label('payload')
    )


@api.route('/account', defaults={'email': None})
@api.route('/accounts/<email>')
@login_required
def get_info(email):
    """Get information about an account.
    If the e-mail is not passed, return information about self."""
    csrf_token = None
    if email is None:
        user = current_user
        if 'csrf_token' not in session:
            abort(403)
        csrf_token = session['csrf_token']
    else:
        if not current_user.is_admin and email != current_user.email:
            abort(403)
        user = Account.query.get_or_404(email)

    out_schema = AccountSchema(exclude=('moderated_projects', 'created_projects', 'stock_changes',
                                        'transactions', 'applications', 'reports',
                                        'notification_settings', 'static_files'),
                               context={'csrf_token': csrf_token})
    return out_schema.jsonify(user)


@api.route('/accounts/groups')
@admin_required
def list_groups():
    """Return a list of all existing groups of users."""
    groups = db.session.query(Account.group) \
                .filter(~Account.is_admin,
                        Account.group.isnot(None),
                        Account.group.op('SIMILAR TO')('[BM][0-9]+(-[A-Z]+)?-[0-9]+')) \
                .group_by(Account.group) \
                .order_by(Account.group)
    return jsonify([row[0] for row in groups.all()])


@api.route('/accounts')
@login_required
def list_users():
    """List all user accounts on the website."""
    default_page = 1
    default_limit = 25

    try:
        limit = int(request.args.get('limit', default_limit))
        page = int(request.args.get('page', default_page))
    except ValueError:
        abort(400, {'message': 'Bad query parameters.'})

    db_query = db.session.query(Account.email, Account.full_name)
    count_query = db.session.query(db.func.count(Account.email))
    if 'q' in request.args:
        like_query = f'%{request.args["q"]}%'
        db_query = db_query.filter(
            or_(Account.email.ilike(like_query),
                Account.full_name.ilike(like_query))
        )
        count_query = count_query.filter(
            or_(Account.email.ilike(like_query),
                Account.full_name.ilike(like_query))
        )

    if limit < 1 or page < 1:
        abort(400, {'message': 'Limit and page number must be positive.'})

    db_query = db_query.order_by(Account.email.asc())
    db_query = db_query.offset(limit * (page - 1)).limit(limit)

    schema = AccountSchema(many=True, only=('email', 'full_name'))
    return jsonify(pages=math.ceil(count_query.scalar() / limit),
                   data=schema.dump(db_query.all()))


# @api.route('/accounts/<string:email>/balance', methods=['PATCH'])
# @admin_required
def change_balance(email):
    """Change a user's balance."""
    if not isinstance(request.json.get('change'), int):
        abort(400, {'message': 'The change in innopoints must be specified as an integer.'})

    user = Account.query.get_or_404(email)
    if request.json['change'] != 0:
        new_transaction = Transaction(account=user,
                                      change=request.json['change'])
        db.session.add(new_transaction)
        try:
            db.session.commit()
        except IntegrityError as err:
            db.session.rollback()
            log.exception(err)
            abort(400, {'message': 'Data integrity violated.'})

        notify(user.email, NotificationType.manual_transaction, {
            'transaction_id': new_transaction.id,
        })

    return NO_PAYLOAD


@api.route('/account/timeline', defaults={'email': None})
@api.route('/accounts/<email>/timeline')
@login_required
def get_timeline(email):
    """Get the timeline of the account.
    If the e-mail is not passed, return own timeline."""
    if email is None:
        user = current_user
    else:
        if not current_user.is_admin and email != current_user.email:
            abort(401)
        user = Account.query.get_or_404(email)

    if 'start_date' in request.args:
        try:
            start_date = datetime.fromisoformat(request.args['start_date'])
        except ValueError:
            abort(400, {'message': 'The datetime must be in ISO format with timezone.'})

        if start_date.tzinfo is None:
            abort(400, {'message': 'The timezone must be passed.'})
    else:
        start_date = unix_epoch

    if 'end_date' in request.args:
        try:
            end_date = datetime.fromisoformat(request.args['end_date'])
        except ValueError:
            abort(400, {'message': 'The datetime must be in ISO format with timezone.'})

        if end_date.tzinfo is None:
            abort(400, {'message': 'The timezone must be passed.'})
    else:
        end_date = tz_aware_now()

    # pylint: disable=invalid-unary-operand-type

    applications = (
        db.session
            .query(Application.id.label('application_id'),
                   Application.status.label('application_status'))
            .add_columns(Application.application_time.label('entry_time'))
            .filter(Application.applicant == user)
            .join(Application.activity)
            .add_columns(Activity.name.label('activity_name'),
                         Activity.id.label('activity_id'))
            .filter(~Activity.internal)
            .join(Activity.project)
            .add_columns(Project.name.label('project_name'),
                         Project.id.label('project_id'),
                         Project.lifetime_stage.label('project_stage'))
            .outerjoin(Application.feedback)
            .add_columns(Feedback.application_id.label('feedback_id'),
                         (Application.actual_hours * Activity.reward_rate).label('reward'))
    )

    purchases = (
        db.session
            .query(StockChange.id.label('stock_change_id'),
                   StockChange.status.label('stock_change_status'),
                   StockChange.time.label('entry_time'))
            .filter(StockChange.account == user)
            .filter(StockChange.amount < 0)
            .join(StockChange.variety).join(Variety.product)
            .add_columns(Product.id.label('product_id'),
                         Product.name.label('product_name'),
                         Product.type.label('product_type'))
    )

    promotions = (
        # pylint: disable=unsubscriptable-object
        db.session
            .query(Notification.payload['project_id'].label('project_id'),
                   Notification.timestamp.label('entry_time'))
            .filter(Notification.recipient == user,
                    Notification.type == NotificationType.added_as_moderator)
            .join(Project,
                  Project.id == Notification.payload.op('->>')('project_id').cast(db.Integer))
            .filter(Project.creator != user, Project.lifetime_stage != LifetimeStage.draft)
            .add_columns(Project.name.label('project_name'))
            .outerjoin(Project.activities.and_(Activity.internal,
                                               Activity.name == '[[Moderation]]'))
            .outerjoin(Activity.applications.and_(Application.applicant == user))
            .add_columns(Application.id.label('application_id'))
    )

    projects = (
        db.session
            .query(Project.id.label('project_id'),
                   Project.name.label('project_name'),
                   Project.review_status,
                   Project.creation_time.label('entry_time'))
            .filter(Project.creator == user,
                    Project.lifetime_stage != LifetimeStage.draft)
    )

    timeline = subquery_to_events(
        applications.filter(Application.application_time >= start_date,
                            Application.application_time <= end_date).subquery('application_events'),
        'application',
    ).union(
        subquery_to_events(
            purchases.filter(StockChange.time >= start_date,
                             StockChange.time <= end_date).subquery('purchase_events'),
            'purchase'
        ),
        subquery_to_events(
            promotions.filter(Notification.timestamp >= start_date,
                              Notification.timestamp <= end_date).subquery('promotion_events'),
            'promotion'
        ),
        subquery_to_events(
            projects.filter(Project.creation_time >= start_date,
                            Project.creation_time <= end_date).subquery('project_events'),
            'project'
        )
    ).subquery()
    ordered_timeline = db.session.query(timeline).order_by(timeline.c.entry_time.desc())

    leftover_applications = db.session.query(
        applications.filter(Application.application_time <= start_date).exists()
    ).scalar()
    leftover_purchases = db.session.query(
        purchases.filter(StockChange.time <= start_date).exists()
    ).scalar()
    leftover_promotions = db.session.query(
        promotions.filter(Notification.timestamp <= start_date).exists()
    ).scalar()
    leftover_projects = db.session.query(
        projects.filter(Project.creation_time <= start_date).exists()
    ).scalar()

    out_schema = TimelineSchema(many=True)
    return jsonify(data=out_schema.dump(ordered_timeline.all()),
                   more=any((leftover_applications,
                             leftover_purchases,
                             leftover_promotions,
                             leftover_projects)))


@api.route('/account/statistics', defaults={'email': None})
@api.route('/accounts/<email>/statistics')
@login_required
def get_statistics(email):
    """Get the statistics of the account.
    If the e-mail is not passed, return own statistics."""
    if email is None:
        user = current_user
    else:
        if not current_user.is_admin and email != current_user.email:
            abort(401)
        user = Account.query.get_or_404(email)

    if 'start_date' in request.args:
        try:
            start_date = datetime.fromisoformat(request.args['start_date'])
        except ValueError:
            abort(400, {'message': 'The datetime must be in ISO format with timezone.'})

        if start_date.tzinfo is None:
            abort(400, {'message': 'The timezone must be passed.'})
    else:
        start_date = unix_epoch

    if 'end_date' in request.args:
        try:
            end_date = datetime.fromisoformat(request.args['end_date'])
        except ValueError:
            abort(400, {'message': 'The datetime must be in ISO format with timezone.'})

        if end_date.tzinfo is None:
            abort(400, {'message': 'The timezone must be passed.'})
    else:
        end_date = tz_aware_now()

    volunteering = (
        # pylint: disable=invalid-unary-operand-type
        db.session.query(db.func.sum(Application.actual_hours),
                         db.func.count(Application.id))
            .filter(Application.applicant == user,
                    Application.status == ApplicationStatus.approved,
                    Application.application_time >= start_date,
                    Application.application_time <= end_date)
            .join(Application.activity).filter(~Activity.fixed_reward, ~Activity.internal)
            .join(Activity.project).filter(Project.lifetime_stage == LifetimeStage.finished)
    ).one()

    rating = (
        db.session.query(db.func.avg(VolunteeringReport.rating))
            .join(VolunteeringReport.application)
            .filter(Application.applicant == user,
                    Application.status == ApplicationStatus.approved,
                    Application.application_time >= start_date,
                    Application.application_time <= end_date)
    ).scalar()

    competences = (
        db.session.query(db.func.count(feedback_competence.c.feedback_id),
                         feedback_competence.c.competence_id)
            .group_by(feedback_competence.c.competence_id)
            .join(Feedback,
                  feedback_competence.c.feedback_id == Feedback.application_id)
            .join(Feedback.application)
            .filter(Application.applicant == user,
                    Application.application_time >= start_date,
                    Application.application_time <= end_date)
            .join(Competence,
                  feedback_competence.c.competence_id == Competence.id)
            .add_columns(Competence.name)
            .group_by(Competence.name)
    ).all()

    return jsonify(hours=volunteering[0] or 0,
                   positions=volunteering[1],
                   rating=float(rating or 0),
                   competences=[dict(zip(('amount', 'id', 'name'), competence))
                                for competence in competences])


@api.route('/account/notification_settings', defaults={'email': None})
@api.route('/accounts/<email>/notification_settings')
@login_required
def get_notification_settings(email):
    """Get the notification settings of the account.
    If the e-mail is not passed, return own settings."""
    if email is None:
        user = current_user
    else:
        if not current_user.is_admin and email != current_user.email:
            abort(401)
        user = Account.query.get_or_404(email)

    out_schema = NotificationSettingsSchema()
    return out_schema.jsonify(user.notification_settings)


# @api.route('/accounts/<email>/notify', methods=['POST'])
# @admin_required
def service_notification(email):
    """Sends a custom service notification by the admin to any user."""
    user = Account.query.get_or_404(email)

    if not request.json.get('message'):
        abort(400, {'message': 'Specify a valid message.'})

    notification = notify(user.email, NotificationType.service, {
        'message': request.json['message']
    })

    if notification is None:
        abort(500, {'message': 'Error creating notification.'})

    return NO_PAYLOAD


# @api.route('/account/telegram', methods=['PATCH'], defaults={'email': None})
# @api.route('/accounts/<email>/telegram', methods=['PATCH'])
# @login_required
def change_telegram(email):
    """Change a user's Telegram username.
    If the email is not passed, change own username."""
    if email is None:
        user = current_user
    else:
        if not current_user.is_admin and email != current_user.email:
            abort(401)
        user = Account.query.get_or_404(email)

    if 'telegram_username' not in request.json:
        abort(400, {'message': 'The telegram_username field must be passed.'})

    user.telegram_username = request.json['telegram_username']
    try:
        db.session.commit()
    except IntegrityError as err:
        db.session.rollback()
        log.exception(err)
        abort(400, {'message': 'Data integrity violated.'})

    return NO_PAYLOAD


# @api.route('/account/notification_settings', methods=['PATCH'], defaults={'email': None})
# @api.route('/accounts/<email>/notification_settings', methods=['PATCH'])
# @login_required
def change_notification_settings(email):
    """Get the notification settings of the account.
    If the e-mail is not passed, return own settings."""
    if email is None:
        user = current_user
    else:
        if not current_user.is_admin and email != current_user.email:
            abort(401)
        user = Account.query.get_or_404(email)

    in_schema = NotificationSettingsSchema()
    try:
        new_notification_settings = in_schema.load(request.json)
    except ValidationError as err:
        abort(400, {'message': err.messages})

    user.notification_settings.update(new_notification_settings)
    flag_modified(user, 'notification_settings')

    try:
        db.session.commit()
    except IntegrityError as err:
        db.session.rollback()
        log.exception(err)
        abort(400, {'message': 'Data integrity violated.'})

    return NO_PAYLOAD

# @api.route('/reclaim-innopoints', methods=['POST'])
# @login_required
def reclaim_innopoints():
    """Get the innopoints the user had on the old system."""
    if 'email' not in request.json or 'password' not in request.json:
        abort(400, {'message': 'Email/username and password should be specified.'})

    conn = sqlite3.connect('db.sqlite3')
    conn.row_factory = lambda c, r: dict(sqlite3.Row(c, r))
    cur = conn.cursor()
    cur.execute('SELECT email, password, points FROM User WHERE email=? OR username=?;',
                (request.json['email'],)*2)
    user = cur.fetchone()

    if user is None:
        abort(403, {'message': 'This email/username is not associated with any account.'})

    if not check_password_hash(user['password'], request.json['password']):
        abort(403, {'message': 'Incorrect password.'})

    if user['points'] != 0:
        new_transaction = Transaction(account=current_user,
                                      change=user['points'])
        db.session.add(new_transaction)
        try:
            db.session.commit()
        except IntegrityError as err:
            db.session.rollback()
            log.exception(err)
            abort(400, {'message': 'Data integrity violated.'})

    cur.execute('DELETE FROM User WHERE email=?;', (user['email'],))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify(user['points'])
