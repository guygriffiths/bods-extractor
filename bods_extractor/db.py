import sqlite3

DB_PATH = 'data/output/fares.db'

def get_connection():
    return sqlite3.connect(DB_PATH)

def init_db():
    with get_connection() as conn:
        conn.executescript('''
            -- Drop the old table to force the schema update for the commuter_score
            DROP TABLE IF EXISTS fare_matrix;
            
            CREATE TABLE IF NOT EXISTS stops (
                atco_code TEXT PRIMARY KEY, 
                easting REAL, 
                northing REAL
            );
            
            CREATE TABLE IF NOT EXISTS zones (
                operator_id TEXT, 
                zone_id TEXT, 
                atco_code TEXT, 
                PRIMARY KEY (operator_id, zone_id, atco_code)
            );
            
            CREATE TABLE IF NOT EXISTS fare_matrix (
                operator_id TEXT, 
                origin_zone TEXT, 
                destination_zone TEXT, 
                ticket_type TEXT, 
                price REAL, 
                commuter_score INTEGER
            );
            
            -- Speed-up indices (No more full-table scans)
            CREATE INDEX IF NOT EXISTS idx_stops_location ON stops (easting, northing);
            CREATE INDEX IF NOT EXISTS idx_zones_atco ON zones (atco_code);
            CREATE INDEX IF NOT EXISTS idx_fare_operator ON fare_matrix (operator_id);
        ''')