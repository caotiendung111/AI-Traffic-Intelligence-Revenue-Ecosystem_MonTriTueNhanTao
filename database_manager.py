import datetime
import sqlite3

class DatabaseManager:
    def __init__(self, db_path='traffic_monitoring.db'):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        # Create table if not exists
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                plate_text TEXT,
                vehicle_type TEXT,
                speed_kmh REAL,
                direction TEXT,
                confidence REAL,
                payment_status TEXT DEFAULT 'UNPAID',
                package_type TEXT DEFAULT 'STANDARD',
                blacklist_reason TEXT,
                lane TEXT,
                deleted_at DATETIME
            )
        ''')
        migrations = [
            "ALTER TABLE detections ADD COLUMN payment_status TEXT DEFAULT 'UNPAID'",
            "ALTER TABLE detections ADD COLUMN package_type TEXT DEFAULT 'STANDARD'",
            "ALTER TABLE detections ADD COLUMN blacklist_reason TEXT",
            "ALTER TABLE detections ADD COLUMN lane TEXT",
            "ALTER TABLE detections ADD COLUMN deleted_at DATETIME",
        ]
        for sql in migrations:
            try:
                cursor.execute(sql)
            except sqlite3.OperationalError:
                pass
        cursor.execute("UPDATE detections SET package_type = 'STANDARD' WHERE package_type IS NULL OR package_type = '' OR package_type = 'NONE'")
        
        conn.commit()
        conn.close()

    def log_detection(self, plate_text, vehicle_type, speed_kmh, direction, confidence):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Check if this plate already exists to preserve payment status
            cursor.execute('SELECT payment_status, package_type FROM detections WHERE plate_text = ? ORDER BY id DESC LIMIT 1', (plate_text,))
            existing = cursor.fetchone()
            
            p_status = existing[0] if existing else 'UNPAID'
            p_type = existing[1] if existing else 'STANDARD'

            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('''
                INSERT INTO detections (timestamp, plate_text, vehicle_type, speed_kmh, direction, confidence, payment_status, package_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (timestamp, plate_text, vehicle_type, speed_kmh, direction, confidence, p_status, p_type))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Database error: {e}")

    def update_payment(self, plate_text, status, package):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE detections SET payment_status = ?, package_type = ? 
            WHERE plate_text = ?
        ''', (status, package, plate_text))
        conn.commit()
        conn.close()

    def toggle_blacklist(self, plate_text):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT payment_status FROM detections WHERE plate_text = ? AND deleted_at IS NULL LIMIT 1", (plate_text,))
        current = cursor.fetchone()
        new_status = "BLACKLISTED" if current and current[0] != "BLACKLISTED" else "UNPAID"
        cursor.execute("UPDATE detections SET payment_status = ? WHERE plate_text = ? AND deleted_at IS NULL", (new_status, plate_text))
        conn.commit()
        conn.close()
        return new_status

    def is_plate_logged(self, plate_text):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM detections WHERE plate_text = ? AND deleted_at IS NULL', (plate_text,))
            result = cursor.fetchone()
            conn.close()
            return result is not None
        except:
            return False

    def get_all_vehicles(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        # Get unique plates with their latest status
        cursor.execute('''
            SELECT plate_text, vehicle_type, MAX(timestamp), payment_status, package_type
            FROM detections
            WHERE deleted_at IS NULL
            GROUP BY plate_text
            ORDER BY timestamp DESC
        ''')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_recent_history(self, limit=5):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT plate_text, vehicle_type, speed_kmh, timestamp
                FROM detections
                WHERE deleted_at IS NULL
                ORDER BY id DESC
                LIMIT ?
            ''', (limit,))
            rows = cursor.fetchall()
            conn.close()
            return rows
        except:
            return []

    def get_total_count(self):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(DISTINCT plate_text) FROM detections WHERE deleted_at IS NULL')
            count = cursor.fetchone()[0]
            conn.close()
            return count
        except:
            return 0
