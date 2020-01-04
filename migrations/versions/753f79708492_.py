"""empty message

Revision ID: 753f79708492
Revises: 133713371338
Create Date: 2020-01-03 23:40:14.083176

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '753f79708492'
down_revision = '133713371338'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('reports', sa.Column('time', sa.DateTime(timezone=True), nullable=False, server_default='now()'))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('reports', 'time')
    # ### end Alembic commands ###
