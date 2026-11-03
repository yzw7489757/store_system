"""正式作业片区、网格、门店状态与巡店日志；仅新增bd_*。"""
from alembic import op
import sqlalchemy as sa
revision = 'b7d2e4f601ab'
down_revision = 'ab7f3c9d21e0'

def upgrade():
    op.create_table('bd_zone_release',
        sa.Column('id',sa.Integer, primary_key=True),
        sa.Column('fingerprint',sa.String(64), nullable=False, unique=True),
        sa.Column('source',sa.JSON, nullable=False),
        sa.Column('parameters',sa.JSON, nullable=False),
        sa.Column('audit',sa.JSON, nullable=False),
        sa.Column('reviews',sa.JSON, nullable=False),
        sa.Column('created_by',sa.Integer, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at',sa.DateTime, nullable=False),
    )
    op.create_table('bd_zone',
        sa.Column('id',sa.Integer, primary_key=True),
        sa.Column('release_id',sa.Integer, sa.ForeignKey('bd_zone_release.id'), nullable=False),
        sa.Column('source_id',sa.String(64), nullable=False),
        sa.Column('name',sa.String(128), nullable=False),
        sa.Column('geometry',sa.JSON, nullable=False),
        sa.Column('area_km2',sa.Float, nullable=False),
        sa.Column('store_count',sa.Integer, nullable=False),
        sa.Column('workload',sa.Float),
        sa.Column('review',sa.JSON, nullable=False),
        sa.Column('state',sa.String(16), nullable=False),
        sa.Column('team_id',sa.Integer, sa.ForeignKey('bd_team.id')),
        sa.Column('leader_id',sa.Integer, sa.ForeignKey('users.id')),
        sa.Column('revision',sa.Integer, nullable=False),
        sa.Column('updated_at',sa.DateTime, nullable=False),
        sa.UniqueConstraint('release_id','source_id',name='uq_bd_zone_release_source'),
    )
    op.create_index('ix_bd_zone_release_id','bd_zone',['release_id'])
    op.create_index('ix_bd_zone_team_id','bd_zone',['team_id'])
    op.create_table('bd_work_grid',
        sa.Column('id',sa.Integer, primary_key=True),
        sa.Column('zone_id',sa.Integer, sa.ForeignKey('bd_zone.id'), nullable=False),
        sa.Column('source_id',sa.String(64), nullable=False),
        sa.Column('geometry',sa.JSON, nullable=False),
        sa.Column('area_km2',sa.Float, nullable=False),
        sa.Column('store_count',sa.Integer, nullable=False),
        sa.Column('state',sa.String(16), nullable=False),
        sa.Column('assignee_id',sa.Integer, sa.ForeignKey('users.id')),
        sa.Column('progress',sa.Integer, nullable=False),
        sa.Column('task_state',sa.String(16), nullable=False),
        sa.Column('revision',sa.Integer, nullable=False),
        sa.Column('updated_at',sa.DateTime, nullable=False),
        sa.UniqueConstraint('zone_id','source_id',name='uq_bd_grid_source'),
    )
    op.create_index('ix_bd_work_grid_zone_id','bd_work_grid',['zone_id'])
    op.create_index('ix_bd_work_grid_assignee_id','bd_work_grid',['assignee_id'])
    op.create_table('bd_grid_store',
        sa.Column('id',sa.Integer, primary_key=True),
        sa.Column('release_id',sa.Integer, sa.ForeignKey('bd_zone_release.id'), nullable=False),
        sa.Column('grid_id',sa.Integer, sa.ForeignKey('bd_work_grid.id'), nullable=False),
        sa.Column('source_id',sa.String(128), nullable=False),
        sa.UniqueConstraint('release_id','source_id',name='uq_bd_grid_store_release_source'),
    )
    op.create_index('ix_bd_grid_store_grid_id','bd_grid_store',['grid_id'])
    op.create_table('bd_work_store',
        sa.Column('id',sa.String(128), primary_key=True),
        sa.Column('name',sa.Text, nullable=False),
        sa.Column('address',sa.Text, nullable=False),
        sa.Column('lat',sa.Float, nullable=False),
        sa.Column('lon',sa.Float, nullable=False),
        sa.Column('status',sa.String(16), nullable=False),
        sa.Column('revision',sa.Integer, nullable=False),
        sa.Column('updated_at',sa.DateTime, nullable=False),
    )
    op.create_table('bd_store_visit_log',
        sa.Column('id',sa.Integer, primary_key=True),
        sa.Column('request_id',sa.String(64), nullable=False, unique=True),
        sa.Column('store_id',sa.String(128), sa.ForeignKey('bd_work_store.id'), nullable=False),
        sa.Column('grid_id',sa.Integer, sa.ForeignKey('bd_work_grid.id'), nullable=False),
        sa.Column('actor_id',sa.Integer, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('operation',sa.String(32), nullable=False),
        sa.Column('before',sa.JSON, nullable=False),
        sa.Column('after',sa.JSON, nullable=False),
        sa.Column('note',sa.Text, nullable=False),
        sa.Column('created_at',sa.DateTime, nullable=False),
    )
    op.create_index('ix_bd_store_visit_log_store_id','bd_store_visit_log',['store_id'])
    op.create_index('ix_bd_store_visit_log_grid_id','bd_store_visit_log',['grid_id'])

def downgrade():
    op.drop_table('bd_store_visit_log')
    op.drop_table('bd_work_store')
    op.drop_table('bd_grid_store')
    op.drop_table('bd_work_grid')
    op.drop_table('bd_zone')
    op.drop_table('bd_zone_release')
