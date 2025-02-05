"""Views related to file management.

StaticFile:
- POST /file
- GET /file/{file_id}
- DELETE /file/{file_id}
"""

import logging
import mimetypes

import requests
import werkzeug
from flask import jsonify, request, current_app
from flask_login import login_required, current_user
from PIL import Image
from sqlalchemy.exc import IntegrityError

from innopoints.blueprints import api
from innopoints.core.file_manager import file_manager
from innopoints.core.helpers import abort, allow_no_json
from innopoints.core.image import crop, shrink
from innopoints.extensions import db
from innopoints.models import StaticFile


ALLOWED_MIMETYPES = {'image/jpeg', 'image/png', 'image/webp'}
NO_PAYLOAD = ('', 204)
log = logging.getLogger(__name__)


def get_mimetype(file: werkzeug.datastructures.FileStorage) -> str:
    """Return a MIME type of a Flask file object"""
    if file.mimetype:
        return file.mimetype

    return mimetypes.guess_type(file.filename)[0]


# @allow_no_json
# @api.route('/file', methods=['POST'])
# @login_required
def upload_file():
    """Upload a file."""
    if 'file' not in request.files:
        abort(400, {'message': 'No file attached.'})

    file = request.files['file']

    if not file.filename:
        abort(400, {'message': 'The file doesn\'t have a name.'})

    mimetype = get_mimetype(file)
    if mimetype not in ALLOWED_MIMETYPES:
        abort(400, {'message': f'Mimetype "{mimetype}" is not allowed.'})

    image = shrink(
        crop(
            Image.open(file.stream),
            request.form
        )
    )

    new_file = StaticFile(mimetype='image/webp', owner=current_user)
    db.session.add(new_file)
    db.session.commit()
    try:
        file_manager.store(image, str(new_file.id))
    except (OSError, requests.exceptions.HTTPError) as err:
        log.exception(err)
        db.session.delete(new_file)
        db.session.commit()
        abort(400, {'message': 'Upload failed.'})
    return jsonify(id=new_file.id, url=f'/file/{new_file.id}')


@api.route('/file/<int:file_id>')
def retrieve_file(file_id):
    """Get the chosen static file."""
    file = StaticFile.query.get_or_404(file_id)
    try:
        file_data = file_manager.retrieve(str(file.id))
    except FileNotFoundError:
        abort(404)

    response = current_app.make_response(file_data)
    response.headers.set('Content-Type', file.mimetype)
    return response


# @api.route('/file/<int:file_id>', methods=['DELETE'])
# @login_required
def delete_file(file_id):
    """Delete the given file by ID."""
    file = StaticFile.query.get_or_404(file_id)
    if file.owner != current_user:
        abort(401)

    try:
        file_manager.delete(str(file_id))
    except FileNotFoundError:
        abort(404, 'File not found on storage')

    db.session.delete(file)
    try:
        db.session.commit()
    except IntegrityError as err:
        db.session.rollback()
        log.exception(err)
        abort(400, {'message': 'Data integrity violated.'})

    return NO_PAYLOAD
