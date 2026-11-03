"""新片区表与边界展示模式；清空旧数据由有备份的显式重建事务执行。"""
from alembic import op
import sqlalchemy as sa

revision = 'd9f42a310bce'
down_revision = 'c8e31f209abd'
COLS = 'id,release_id,source_id,name,geometry,area_km2,store_count,workload,review,state,team_id,leader_id,revision,updated_at'


def rebind(old, new):
    connection = op.get_bind()
    foreign_keys = sa.inspect(connection).get_foreign_keys('bd_work_grid')
    with op.batch_alter_table('bd_work_grid', naming_convention={
            'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s'}) as batch:
        for foreign_key in foreign_keys:
            if foreign_key['referred_table'] == old:
                batch.drop_constraint(foreign_key['name'] or f'fk_bd_work_grid_zone_id_{old}', type_='foreignkey')
        batch.create_foreign_key(f'fk_bd_work_grid_{new}', new, ['zone_id'], ['id'])


def reseed(table):
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(sa.text(f"SELECT setval(pg_get_serial_sequence('{table}','id'), "
                           f"COALESCE((SELECT MAX(id) FROM {table}),1), EXISTS(SELECT 1 FROM {table}))"))


def upgrade():
    op.create_table('bd_region',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('release_id', sa.Integer, sa.ForeignKey('bd_zone_release.id'), nullable=False),
        sa.Column('source_id', sa.String(64), nullable=False),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('geometry', sa.JSON, nullable=False),
        sa.Column('merge_boundaries', sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column('area_km2', sa.Float, nullable=False),
        sa.Column('store_count', sa.Integer, nullable=False),
        sa.Column('workload', sa.Float), sa.Column('review', sa.JSON, nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('team_id', sa.Integer, sa.ForeignKey('bd_team.id')),
        sa.Column('leader_id', sa.Integer, sa.ForeignKey('users.id')),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('updated_at', sa.DateTime, nullable=False),
        sa.UniqueConstraint('release_id', 'source_id', name='uq_bd_region_release_source'))
    for column in ('release_id', 'team_id'):
        op.create_index(f'ix_bd_region_{column}', 'bd_region', [column])
    op.execute(sa.text(f'INSERT INTO bd_region ({COLS}) SELECT {COLS} FROM bd_zone'))
    rebind('bd_zone', 'bd_region')
    reseed('bd_region')


def downgrade():
    op.execute(sa.text('DELETE FROM bd_zone'))
    op.execute(sa.text(f'INSERT INTO bd_zone ({COLS}) SELECT {COLS} FROM bd_region'))
    rebind('bd_region', 'bd_zone')
    reseed('bd_zone')
    op.drop_table('bd_region')
