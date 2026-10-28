SELECT json_build_object(
  'time', clock_timestamp(),
  'settings', (SELECT json_object_agg(name, setting) FROM pg_settings WHERE name IN
    ('max_connections','shared_buffers','effective_cache_size','work_mem','maintenance_work_mem',
     'synchronous_commit','synchronous_standby_names','fsync','full_page_writes','max_wal_size',
     'track_io_timing','track_wal_io_timing','max_worker_processes')),
  'database_bytes', pg_database_size(current_database()),
  'jobs', (SELECT json_agg(x) FROM (SELECT kind,status,count(*) FROM jobs GROUP BY 1,2) x),
  'waits', (SELECT json_agg(x) FROM (SELECT backend_type,state,wait_event_type,wait_event,count(*)
    FROM pg_stat_activity GROUP BY 1,2,3,4) x),
  'database', (SELECT row_to_json(x) FROM pg_stat_database x WHERE datname=current_database()),
  'wal', (SELECT row_to_json(x) FROM pg_stat_wal x),
  'io', (SELECT json_agg(x) FROM pg_stat_io x),
  'replication', (SELECT json_agg(x) FROM (SELECT application_name,state,sync_state,
    write_lag,flush_lag,replay_lag,pg_wal_lsn_diff(pg_current_wal_lsn(),replay_lsn) AS lag_bytes
    FROM pg_stat_replication) x),
  'queries', (SELECT json_agg(x) FROM (SELECT queryid,calls,total_exec_time,rows,
    shared_blks_hit,shared_blks_read,temp_blks_written,wal_bytes
    FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 25) x)
);
