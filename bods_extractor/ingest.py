import zipfile, io, csv, sqlite3, re
from lxml import etree
from .db import get_connection

class IngestionEngine:
    def __init__(self, target_operator=None):
        self.target_operator = target_operator
        self.conn = get_connection()

    def ingest_naptan(self, csv_path):
        """Loads ATCO codes and coordinates from Stops.csv"""
        cursor = self.conn.cursor()
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            data = [(row['ATCOCode'], row.get('Easting', 0), row.get('Northing', 0)) for row in reader if row.get('ATCOCode')]
            cursor.executemany('INSERT OR REPLACE INTO stops (atco_code, easting, northing) VALUES (?, ?, ?)', data)
        self.conn.commit()
        print(f"[i] Loaded {len(data)} stop(s) from {csv_path}.")
        if not data:
            print(f"[!] Warning: 0 stops loaded -- check {csv_path} has an ATCOCode column and isn't empty.")

    def process_archive(self, archive_path):
        target_lower = self.target_operator.lower() if self.target_operator else None
        matched_folders = set()
        files_processed = 0
        totals = {"zone_tags_seen": 0, "price_tags_seen": 0, "zone_rows_written": 0, "fare_rows_written": 0}

        def _accumulate_totals(stats):
            for key in totals:
                totals[key] += stats[key]

        with zipfile.ZipFile(archive_path, 'r') as master_zip:
            for file_info in master_zip.infolist():
                path_parts = file_info.filename.split('/')
                if len(path_parts) < 2:
                    continue

                operator_id = path_parts[0]

                if target_lower and target_lower not in operator_id.lower():
                    continue

                entry_name = file_info.filename
                if entry_name.endswith('.zip'):
                    matched_folders.add(operator_id)
                    files_processed += 1
                    nested_zip_data = master_zip.read(entry_name)
                    with zipfile.ZipFile(io.BytesIO(nested_zip_data)) as nested_zip:
                        for xml_file in nested_zip.namelist():
                            if xml_file.endswith('.xml'):
                                with nested_zip.open(xml_file) as xml_stream:
                                    _accumulate_totals(self._parse_netex_stream(xml_stream, operator_id))
                elif entry_name.endswith('.xml'):
                    matched_folders.add(operator_id)
                    files_processed += 1
                    with master_zip.open(entry_name) as xml_stream:
                        _accumulate_totals(self._parse_netex_stream(xml_stream, operator_id))

        cursor = self.conn.cursor()
        zone_count = cursor.execute('SELECT COUNT(*) FROM zones').fetchone()[0]
        fare_count = cursor.execute('SELECT COUNT(*) FROM fare_matrix').fetchone()[0]

        print(f"[i] Matched {len(matched_folders)} operator folder(s). Processed {files_processed} file(s).")
        print(f"[i] Zones written: {totals['zone_rows_written']} | Fares written: {totals['fare_rows_written']}")
        print(f"[i] Database totals -> zones: {zone_count}, fare_matrix: {fare_count}")

    @staticmethod
    def _collect_metadata(xml_bytes):
        """Build map of DistanceMatrixElements AND FareProducts to their real Names."""
        matrix_elements = {}
        fare_products = {}
        
        context = etree.iterparse(io.BytesIO(xml_bytes), events=('end',))
        for event, elem in context:
            tag = elem.tag.split('}')[-1]
            if tag == 'DistanceMatrixElement':
                element_id = elem.get('id')
                start_ref_elem = elem.find('.//{*}StartTariffZoneRef')
                end_ref_elem = elem.find('.//{*}EndTariffZoneRef')
                start_zone = start_ref_elem.get('ref') if start_ref_elem is not None else None
                end_zone = end_ref_elem.get('ref') if end_ref_elem is not None else None
                if element_id and start_zone and end_zone:
                    matrix_elements[element_id] = (start_zone, end_zone)
            elif tag in ['PreassignedFareProduct', 'FareProduct', 'ValidableElement']:
                prod_id = elem.get('id')
                name_elem = elem.find('.//{*}Name')
                if prod_id and name_elem is not None and name_elem.text:
                    fare_products[prod_id] = name_elem.text.strip()
        return matrix_elements, fare_products

    def _parse_netex_stream(self, xml_stream, operator_noc):
        xml_bytes = xml_stream.read()
        matrix_elements, fare_products = self._collect_metadata(xml_bytes)
        
        zone_tags_seen = 0
        price_tags_seen = 0
        zone_rows_written = 0
        fare_rows_written = 0

        context = etree.iterparse(io.BytesIO(xml_bytes), events=('end',))
        cursor = self.conn.cursor()

        for event, elem in context:
            tag = elem.tag.split('}')[-1]

            if tag in ['FareZone', 'TariffZone']:
                zone_tags_seen += 1
                current_zone_id = elem.get('id')
                for stop in elem.findall('.//{*}ScheduledStopPointRef'):
                    atco = stop.get('ref')
                    if current_zone_id and atco:
                        clean_atco = atco.replace('atco:', '').replace('naptan:', '')
                        cursor.execute('INSERT OR IGNORE INTO zones VALUES (?, ?, ?)', (operator_noc, current_zone_id, clean_atco))
                        zone_rows_written += 1

            if tag in ['DistanceMatrixElementPrice', 'FarePrice', 'Cell', 'Precio', 'Price']:
                price_tags_seen += 1
                
                # Check for explicit rejection flag
                is_allowed_elem = elem.find('.//{*}IsAllowed')
                if is_allowed_elem is not None and is_allowed_elem.text and is_allowed_elem.text.lower() == 'false':
                    continue

                elem_id = elem.get('id', '')
                
                ref_elem = elem.find('.//{*}DistanceMatrixElementRef')
                matrix_id = ref_elem.get('ref') if ref_elem is not None else ""
                
                prod_ref_elem = elem.find('.//{*}PreassignedFareProductRef') or elem.find('.//{*}FareProductRef')
                prod_ref_id = prod_ref_elem.get('ref') if prod_ref_elem is not None else ""
                product_name = fare_products.get(prod_ref_id, "")

                geo_ref_elem = elem.find('.//{*}GeographicalIntervalPriceRef')
                geo_ref = geo_ref_elem.get('ref') if geo_ref_elem is not None else ""
                
                class_string = f"{elem_id} {matrix_id} {prod_ref_id} {product_name} {geo_ref}".lower()

                # --- THE COMMUTER CONFIDENCE SCORING ENGINE ---
                score = 0
                
                # Positive adult commuter signals
                if 'adult' in class_string or 'adrtn' in class_string or 'ad_' in class_string: score += 100
                if 'standard' in class_string or 'any' in class_string: score += 50
                if 'return' in class_string or 'rtn' in class_string: score += 30
                if 'day' in class_string or 'daily' in class_string: score += 30
                
                # Boost network-wide passes so they beat restricted local passes
                if 'network' in class_string or 'all' in class_string or 'zone' in class_string: score += 40
                
                # Ruthless demotion for anything that isn't a standard adult ticket
                if any(x in class_string for x in ['child', 'chd', 'student', 'pupil', 'sch', 'senior', 'oap', 'young', 'yp', 'under', 'u19', 'u21', 'dog', 'animal', 'companion', 'family', 'group', 'staff', 'job', 'add-on', 'addon']):
                    score -= 1000

                price_val = None
                
                for child in elem.iter():
                    if child.text and 'amount' in child.tag.lower():
                        try:
                            price_val = float(child.text.strip())
                            break
                        except ValueError:
                            pass
                            
                if price_val is None and geo_ref:
                    match = re.search(r'price_band_(\d+\.?\d*)', geo_ref)
                    if match:
                        price_val = float(match.group(1))

                if price_val is None:
                    match = re.search(r'(?:price_band_|price_|amount_)(\d+\.?\d*)', elem_id)
                    if match:
                        price_val = float(match.group(1))

                if price_val is None:
                    continue 

                origin_zone, dest_zone = matrix_elements.get(matrix_id, (None, None))
                origin_zone = origin_zone or 'ANY_TO_ANY'
                dest_zone = dest_zone or 'ANY_TO_ANY'

                if any(term in class_string for term in ['week', '7day', '7-day', 'period', 'season', '1m', '4w', '28day', 'annual', 'termly']):
                    ticket_type = "Weekly/Period"
                elif any(term in class_string for term in ['day', '24hr', '24-hour', 'pass', 'allmodes', 'rover', 'hopper', 'daily']):
                    ticket_type = "Day Pass"
                elif any(term in class_string for term in ['return', 'rtn', 'two-way', '2trip', 'round-trip']):
                    ticket_type = "Return"
                elif any(term in class_string for term in ['single', 'sgl', 'one-way', '1-way']):
                    ticket_type = "Single"
                else:
                    ticket_type = "Single" if price_val < 5.0 else "Day Pass"

                try:
                    cursor.execute('INSERT INTO fare_matrix VALUES (?, ?, ?, ?, ?, ?)',
                                 (operator_noc, origin_zone, dest_zone, ticket_type, price_val, score))
                    fare_rows_written += 1
                except ValueError:
                    pass

        self.conn.commit()
        return {
            "zone_tags_seen": zone_tags_seen,
            "price_tags_seen": price_tags_seen,
            "zone_rows_written": zone_rows_written,
            "fare_rows_written": fare_rows_written,
        }