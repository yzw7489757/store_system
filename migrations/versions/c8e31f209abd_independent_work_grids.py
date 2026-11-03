"""网格独立于片区；历史版本归属由原片区回填。"""
from alembic import op
import sqlalchemy as sa

revision = 'c8e31f209abd'
down_revision = 'b7d2e4f601ab'


def upgrade():
    op.add_column('bd_work_grid', sa.Column('release_id', sa.Integer(), nullable=True))
    op.execute('UPDATE bd_work_grid SET release_id = '
               '(SELECT release_id FROM bd_zone WHERE bd_zone.id = bd_work_grid.zone_id)')
    with op.batch_alter_table('bd_work_grid') as batch:
        batch.alter_column('release_id', existing_type=sa.Integer(), nullable=False)
        batch.create_foreign_key('fk_bd_grid_release', 'bd_zone_release', ['release_id'], ['id'])
        batch.alter_column('zone_id', existing_type=sa.Integer(), nullable=True)
        batch.create_index('ix_bd_work_grid_release_id', ['release_id'])
        batch.create_unique_constraint('uq_bd_grid_release_source', ['release_id', 'source_id'])


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text('SELECT COUNT(*) FROM bd_work_grid WHERE zone_id IS NULL')).scalar():
        raise RuntimeError('仍有未归片区网格，不能回退迁移')
    with op.batch_alter_table('bd_work_grid') as batch:
        batch.drop_index('ix_bd_work_grid_release_id')
        batch.drop_constraint('uq_bd_grid_release_source', type_='unique')
        batch.drop_constraint('fk_bd_grid_release', type_='foreignkey')
        batch.alter_column('zone_id', existing_type=sa.Integer(), nullable=False)
        batch.drop_column('release_id')
