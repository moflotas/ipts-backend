"""Views related to the Activity model.

Activity:
- POST   /projects/{project_id}/activities
- PATCH  /projects/{project_id}/activities/{activity_id}
- DELETE /projects/{project_id}/activities/{activity_id}
- PATCH  /projects/{project_id}/activities/{activity_id}/publish

Competence:
- GET    /competences
- POST   /competences
- PATCH  /competences/{competence_id}
- DELETE /competences/{competence_id}
"""

import logging

from flask import request
from flask.views import MethodView
from flask_login import login_required, current_user
from marshmallow import ValidationError
from sqlalchemy.exc import IntegrityError

from innopoints.extensions import db
from innopoints.blueprints import api
from innopoints.core.helpers import abort, allow_no_json, admin_required
from innopoints.core.notifications import remove_notifications
from innopoints.models import (
    Activity,
    ApplicationStatus,
    Competence,
    IPTS_PER_HOUR,
    LifetimeStage,
    Project,
)
from innopoints.schemas import ActivitySchema, CompetenceSchema

NO_PAYLOAD = ('', 204)
log = logging.getLogger(__name__)


# @api.route('/projects/<int:project_id>/activities', methods=['POST'])
# @login_required
def create_activity(project_id):
    """Create a new activity to an existing project."""
    project = Project.query.get_or_404(project_id)
    if not current_user.is_admin and current_user not in project.moderators:
        abort(403)

    if project.lifetime_stage not in (LifetimeStage.draft, LifetimeStage.ongoing):
        abort(400, {'message': 'Activities may only be created on draft and ongoing projects.'})

    in_schema = ActivitySchema(exclude=('id', 'project', 'applications', 'internal'))

    try:
        new_activity = in_schema.load(request.json)
    except ValidationError as err:
        abort(400, {'message': err.messages})

    if new_activity.draft is None:
        new_activity.draft = True

    if not new_activity.draft and not new_activity.is_complete:
        abort(400, {'message': 'Incomplete activities cannot be marked as non-draft.'})

    new_activity.project = project

    try:
        db.session.add(new_activity)
        db.session.commit()
    except IntegrityError as err:
        db.session.rollback()
        log.exception(err)
        abort(400, {'message': 'Data integrity violated.'})

    out_schema = ActivitySchema(exclude=('existing_application',),
                                context={'user': current_user})
    return out_schema.jsonify(new_activity)


class ActivityAPI(MethodView):
    """REST views for a particular instance of an Activity model."""

    @login_required
    def patch(self, project_id, activity_id):
        """Edit the activity."""
        project = Project.query.get_or_404(project_id)
        if not current_user.is_admin and current_user not in project.moderators:
            abort(403)

        if project.lifetime_stage not in (LifetimeStage.draft, LifetimeStage.ongoing):
            abort(400, {'message': 'Activities may only be edited on draft and ongoing projects.'})

        activity = Activity.query.get_or_404(activity_id)
        if activity.internal:
            abort(404)

        if activity.project != project:
            abort(400, {'message': 'The specified project and activity are unrelated.'})

        in_schema = ActivitySchema(exclude=('id', 'project', 'applications', 'internal'))

        try:
            with db.session.no_autoflush:
                updated_activity = in_schema.load(request.json, instance=activity, partial=True)
        except ValidationError as err:
            abort(400, {'message': err.messages})

        if not updated_activity.draft and not updated_activity.is_complete:
            abort(400, {'message': 'Incomplete activities cannot be marked as non-draft.'})

        if activity.fixed_reward and activity.working_hours != 1:
            abort(400, {'message': 'Cannot set working hours for fixed activities.'})

        if not activity.fixed_reward and activity.reward_rate != IPTS_PER_HOUR:
            abort(400, {'message': 'The reward rate for hourly activities may not be changed.'})

        with db.session.no_autoflush:
            if updated_activity.people_required is not None:
                if updated_activity.accepted_applications > updated_activity.people_required:
                    abort(400, {'message': 'Cannot reduce the required people '
                                           'beyond the amount of existing applications.'})

                if updated_activity.draft and updated_activity.applications:
                    abort(400, {'message': 'Cannot mark as draft, applications exist.'})

            for application in updated_activity.applications:
                if (updated_activity.application_deadline is not None
                        and updated_activity.application_deadline < application.application_time):
                    abort(400, {'message': 'Cannot set the deadline earlier '
                                           'than the existing application'})
                if application.status != ApplicationStatus.rejected:
                    application.actual_hours = updated_activity.working_hours

        try:
            db.session.add(updated_activity)
            db.session.commit()
        except IntegrityError as err:
            db.session.rollback()
            log.exception(err)
            abort(400, {'message': 'Data integrity violated.'})

        out_schema = ActivitySchema(exclude=('existing_application',),
                                    context={'user': current_user})
        return out_schema.jsonify(updated_activity)

    @login_required
    def delete(self, project_id, activity_id):
        """Delete the activity."""
        project = Project.query.get_or_404(project_id)
        if not current_user.is_admin and current_user not in project.moderators:
            abort(403)

        if project.lifetime_stage not in (LifetimeStage.draft, LifetimeStage.ongoing):
            abort(400, {'message': 'Activities may only be deleted on draft and ongoing projects.'})

        activity = Activity.query.get_or_404(activity_id)
        if activity.internal:
            abort(404)

        if activity.project != project:
            abort(400, {'message': 'The specified project and activity are unrelated.'})

        db.session.delete(activity)

        try:
            db.session.commit()
            remove_notifications({
                'activity_id': activity_id,
            })
        except IntegrityError as err:
            db.session.rollback()
            log.exception(err)
            abort(400, {'message': 'Data integrity violated.'})
        return NO_PAYLOAD


# activity_api = ActivityAPI.as_view('activity_api')
# api.add_url_rule('/projects/<int:project_id>/activities/<int:activity_id>',
#                  view_func=activity_api,
#                  methods=('PATCH', 'DELETE'))


# @allow_no_json
# @api.route('/projects/<int:project_id>/activities/<int:activity_id>/publish', methods=['PATCH'])
# @login_required
def publish_activity(project_id, activity_id):
    """Publish the activity."""
    project = Project.query.get_or_404(project_id)
    if not current_user.is_admin and current_user not in project.moderators:
        abort(403)

    activity = Activity.query.get_or_404(activity_id)
    if activity.internal:
        abort(404)

    if activity.project != project:
        abort(400, {'message': 'The specified project and activity are unrelated.'})

    if (activity.name is None
            or activity.start_date is None
            or activity.end_date is None
            or activity.start_date > activity.end_date):
        abort(400, {'message': 'The name or dates of the activity are invalid.'})

    activity.draft = False

    try:
        db.session.commit()
    except IntegrityError as err:
        db.session.rollback()
        log.exception(err)
        abort(400, {'message': 'Data integrity violated.'})

    return NO_PAYLOAD


# ----- Competence -----

@api.route('/competences')
def list_competences():
    """List all of the existing competences."""
    schema = CompetenceSchema(many=True)
    return schema.jsonify(Competence.query.all())


# @api.route('/competences', methods=['POST'])
# @admin_required
def create_competence():
    """Create a new competence."""
    in_schema = CompetenceSchema(exclude=('id',))

    try:
        new_competence = in_schema.load(request.json)
    except ValidationError as err:
        abort(400, {'message': err.messages})

    try:
        db.session.add(new_competence)
        db.session.commit()
    except IntegrityError as err:
        db.session.rollback()
        log.exception(err)
        abort(400, {'message': 'Data integrity violated.'})

    out_schema = CompetenceSchema()
    return out_schema.jsonify(new_competence)


class CompetenceAPI(MethodView):
    """REST views for a particular instance of a Competence model."""

    @admin_required
    def patch(self, compt_id):
        """Edit the competence."""
        competence = Competence.query.get_or_404(compt_id)

        in_schema = CompetenceSchema(exclude=('id',))

        try:
            updated_competence = in_schema.load(request.json, instance=competence, partial=True)
        except ValidationError as err:
            abort(400, {'message': err.messages})

        try:
            db.session.add(updated_competence)
            db.session.commit()
        except IntegrityError as err:
            db.session.rollback()
            log.exception(err)
            abort(400, {'message': 'Data integrity violated.'})

        out_schema = CompetenceSchema()
        return out_schema.jsonify(updated_competence)

    @admin_required
    def delete(self, compt_id):
        """Delete the competence."""
        competence = Competence.query.get_or_404(compt_id)

        try:
            db.session.delete(competence)
            db.session.commit()
        except IntegrityError as err:
            db.session.rollback()
            log.exception(err)
            abort(400, {'message': 'Data integrity violated.'})
        return NO_PAYLOAD


# competence_api = CompetenceAPI.as_view('competence_api')
# api.add_url_rule('/competences/<int:compt_id>',
#                  view_func=competence_api,
#                  methods=('PATCH', 'DELETE'))
