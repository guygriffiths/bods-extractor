"""
Run this locally from your bods2 project root:

    python3 diagnose_netex.py

It finds the first FareZone/TariffZone element and the first
DistanceMatrixElementPrice/FarePrice element inside the operator folder
that matched last time (BorderBus Ltd_46), and pretty-prints their full
structure. Paste the output back -- that tells us exactly what tag/attribute
names to use instead of guessing again.
"""
import zipfile
import io
from lxml import etree

ARCHIVE_PATH = 'data/raw/bodds_fares_archive_20260803.zip'
TARGET_FOLDER_SUBSTRING = 'BorderBus'  # last folder that actually matched and parsed
WANT_TAGS = {'FareZone', 'TariffZone', 'DistanceMatrixElementPrice', 'FarePrice'}


def dump_first_matches(xml_bytes, source_label):
    found = set()
    context = etree.iterparse(io.BytesIO(xml_bytes), events=('end',))
    for event, elem in context:
        tag = elem.tag.split('}')[-1]
        if tag in WANT_TAGS and tag not in found:
            found.add(tag)
            print(f"\n{'=' * 70}")
            print(f"First <{tag}> found in {source_label}:")
            print('=' * 70)
            print(etree.tostring(elem, pretty_print=True).decode('utf-8')[:3000])
        if found == WANT_TAGS:
            return True
    return False


def main():
    with zipfile.ZipFile(ARCHIVE_PATH, 'r') as master_zip:
        for file_info in master_zip.infolist():
            if TARGET_FOLDER_SUBSTRING.lower() not in file_info.filename.lower():
                continue

            entry_name = file_info.filename
            if entry_name.endswith('.zip'):
                nested_zip_data = master_zip.read(entry_name)
                with zipfile.ZipFile(io.BytesIO(nested_zip_data)) as nested_zip:
                    for xml_file in nested_zip.namelist():
                        if xml_file.endswith('.xml'):
                            xml_bytes = nested_zip.read(xml_file)
                            if dump_first_matches(xml_bytes, f"{entry_name} -> {xml_file}"):
                                return
            elif entry_name.endswith('.xml'):
                xml_bytes = master_zip.read(entry_name)
                if dump_first_matches(xml_bytes, entry_name):
                    return

    print("Didn't find all target tags -- adjust TARGET_FOLDER_SUBSTRING or check the archive.")


if __name__ == '__main__':
    main()
