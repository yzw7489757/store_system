"""数据库适配器使用显式隔离SQLite源表；绝不查询实际商户/结算表。"""
import copy
import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.pool import StaticPool
from shapely.geometry import Point,shape
from app.services import bd_zone_merchants as m,bd_zones as z
from tests_web.test_bd_zone_coverage import source  # noqa:F401


def rows_for_source():
    rows=[]
    for identity,x,y in [('db-a',100,500),('db-b',1000,600),('db-review',1500,500)]:
        lng,lat=z.geographic(Point(x,y))['coordinates']
        rows.append(dict(place_id=identity,name='synthetic-'+identity,lat=lat,lon=lng))
    return rows


def test_database_rows_replace_not_append_csv_and_keep_geometry(source):
    before=copy.deepcopy(source)
    doc=m.bind_rows(source,rows_for_source(),{'backend':'postgresql','table':m.SOURCE_TABLE,'total_count':3,'read_at':'synthetic'})
    assert source==before
    assert {v for cell in doc['road_cells'] for v in cell['merchant_ids']}=={'db-a','db-b'}
    assert [cell['metric_geometry'] for cell in doc['road_cells']]==[cell['metric_geometry'] for cell in source['road_cells']]
    assert doc['merchant_audit']['inside_boundary']==3 and doc['store_count']==2
    assert doc['review_merchants'][0]['store_id']=='db-review'
    assert doc['source']['table']==m.SOURCE_TABLE


def test_bad_coordinate_and_duplicate_are_not_silently_accepted(source):
    rows=rows_for_source();rows.extend([rows[0],dict(place_id='bad',name='synthetic',lat=None,lon=None)])
    doc=m.bind_rows(source,rows,{'read_at':'synthetic'})
    assert doc['merchant_audit']['source_duplicates']==1
    assert doc['merchant_audit']['invalid_coordinates']==1
    assert doc['merchant_audit']['inside_boundary']==3


def test_real_reader_uses_source_table_and_read_only_transaction(source):
    engine=create_engine('sqlite://',poolclass=StaticPool)
    with engine.begin() as conn:
        conn.exec_driver_sql("ATTACH DATABASE ':memory:' AS public")
        conn.exec_driver_sql('CREATE TABLE public.kanto_merchants_v20261029 (place_id TEXT PRIMARY KEY,name TEXT,lat REAL,lon REAL)')
        conn.execute(text('INSERT INTO public.kanto_merchants_v20261029 VALUES (:place_id,:name,:lat,:lon)'),rows_for_source())
    doc=m.bind_database(source,engine=engine)
    assert doc['source']['total_count']==3 and doc['store_count']==2
    with engine.connect() as conn:
        assert conn.exec_driver_sql('PRAGMA query_only').scalar_one()==1
    engine.dispose()


def test_unavailable_database_fails_without_using_old_snapshot(source):
    engine=create_engine('sqlite://')
    with pytest.raises(z.ZoneDataError,match='数据库商户源读取失败'):
        m.bind_database(source,engine=engine)
