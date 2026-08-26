import zipfile
import io
from lxml import etree

ARCHIVE_PATH = 'data/raw/bodds_fares_archive_20260803.zip'
TARGET_TAGS = {'DistanceMatrixElementPrice', 'FarePrice', 'Cell', 'Precio', 'Price'}

def get_sample_from_xml(xml_bytes):
    """Finds the first matching price tag in the XML and returns its raw string."""
    context = etree.iterparse(io.BytesIO(xml_bytes), events=('end',))
    for event, elem in context:
        tag = elem.tag.split('}')[-1]
        if tag in TARGET_TAGS:
            xml_str = etree.tostring(elem, encoding='unicode', pretty_print=True)
            return tag, xml_str
    return None, None

def main():
    sampled_operators = set()
    
    print("Scanning archive for unique operator pricing structures...")
    
    with zipfile.ZipFile(ARCHIVE_PATH, 'r') as master_zip:
        for file_info in master_zip.infolist():
            path_parts = file_info.filename.split('/')
            if len(path_parts) < 2:
                continue
                
            operator_id = path_parts[0]
            
            # Skip if we already found a sample for this operator
            if operator_id in sampled_operators:
                continue
            
            entry_name = file_info.filename
            sample_found = False
            
            if entry_name.endswith('.zip'):
                nested_zip_data = master_zip.read(entry_name)
                with zipfile.ZipFile(io.BytesIO(nested_zip_data)) as nested_zip:
                    for xml_file in nested_zip.namelist():
                        if xml_file.endswith('.xml'):
                            xml_bytes = nested_zip.read(xml_file)
                            tag, xml_str = get_sample_from_xml(xml_bytes)
                            if xml_str:
                                print(f"\n{'='*80}")
                                print(f"OPERATOR: {operator_id}")
                                print(f"FILE:     {xml_file}")
                                print(f"TAG:      <{tag}>")
                                print(f"{'-'*80}")
                                print(xml_str.strip())
                                sample_found = True
                                break # Found one for this operator, stop checking its files
                                
            elif entry_name.endswith('.xml'):
                xml_bytes = master_zip.read(entry_name)
                tag, xml_str = get_sample_from_xml(xml_bytes)
                if xml_str:
                    print(f"\n{'='*80}")
                    print(f"OPERATOR: {operator_id}")
                    print(f"FILE:     {entry_name}")
                    print(f"TAG:      <{tag}>")
                    print(f"{'-'*80}")
                    print(xml_str.strip())
                    sample_found = True
                    
            if sample_found:
                sampled_operators.add(operator_id)
                
    print(f"\n{'='*80}")
    print(f"[i] Successfully sampled {len(sampled_operators)} operators.")
    print(f"{'='*80}")

if __name__ == '__main__':
    main()