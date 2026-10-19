"""Metadata tags stored as their own columns in the records table.

These are the tags present on every file of the reference library plus the
tags present on at least 48% of its files, plus the offline ExifTool
Geolocation (reverse geocoding) tags City, Region, Country and CountryCode. The full ExifTool output is still
kept in records.metadata_json.
"""

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

TAGS = [
    "ExifTool:ExifToolVersion",
    "ExifTool:GeolocationCity",
    "ExifTool:GeolocationRegion",
    "ExifTool:GeolocationCountry",
    "ExifTool:GeolocationCountryCode",
    "File:ExifByteOrder",
    "File:FileType",
    "File:FileTypeExtension",
    "File:ImageHeight",
    "File:ImageWidth",
    "File:MIMEType",
    "System:Directory",
    "System:FileAccessDate",
    "System:FileInodeChangeDate",
    "System:FileModifyDate",
    "System:FileName",
    "System:FilePermissions",
    "System:FileSize",
    "Composite:GPSAltitude",
    "Composite:GPSLatitude",
    "Composite:GPSLongitude",
    "Composite:GPSPosition",
    "Composite:ImageSize",
    "Composite:Megapixels",
    "QuickTime:CompatibleBrands",
    "QuickTime:HandlerType",
    "QuickTime:MajorBrand",
    "QuickTime:MediaData",
    "QuickTime:MediaDataOffset",
    "QuickTime:MediaDataSize",
    "QuickTime:MinorVersion",
    "ExifIFD:ExifImageHeight",
    "ExifIFD:ExifImageWidth",
    "ICC-header:CMMFlags",
    "ICC-header:ColorSpaceData",
    "ICC-header:ConnectionSpaceIlluminant",
    "ICC-header:DeviceAttributes",
    "ICC-header:DeviceManufacturer",
    "ICC-header:DeviceModel",
    "ICC-header:PrimaryPlatform",
    "ICC-header:ProfileCMMType",
    "ICC-header:ProfileClass",
    "ICC-header:ProfileConnectionSpace",
    "ICC-header:ProfileCreator",
    "ICC-header:ProfileDateTime",
    "ICC-header:ProfileFileSignature",
    "ICC-header:ProfileID",
    "ICC-header:ProfileVersion",
    "ICC-header:RenderingIntent",
    "ICC_Profile:BlueMatrixColumn",
    "ICC_Profile:BlueTRC",
    "ICC_Profile:GreenMatrixColumn",
    "ICC_Profile:GreenTRC",
    "ICC_Profile:MediaWhitePoint",
    "ICC_Profile:ProfileCopyright",
    "ICC_Profile:ProfileDescription",
    "ICC_Profile:RedMatrixColumn",
    "ICC_Profile:RedTRC",
    "IFD0:ModifyDate",
    "IFD0:ResolutionUnit",
    "IFD0:Software",
    "IFD0:XResolution",
    "IFD0:YResolution",
]


# Dates are shown as yyyy-mm-dd hh24:mi:ss in Pacific time (PST in winter, PDT in
# summer). Values that carry a UTC offset are converted; values without a time
# zone (such as EXIF ModifyDate) are camera wall-clock time and are only
# reformatted.
DISPLAY_TIMEZONE = ZoneInfo("America/Los_Angeles")
DATE_TAGS = {
    "System:FileAccessDate",
    "System:FileInodeChangeDate",
    "System:FileModifyDate",
    "IFD0:ModifyDate",
    "ICC-header:ProfileDateTime",
}
_DATE_PATTERN = re.compile(
    r"^(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)


def format_datetime(value):
    """Return value as 'yyyy-mm-dd hh24:mi:ss' in Pacific time, or unchanged."""
    if not isinstance(value, str):
        return value
    match = _DATE_PATTERN.match(value.strip())
    if not match:
        return value
    year, month, day, hour, minute, second, offset = match.groups()
    try:
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
        if offset:
            iso_offset = "+00:00" if offset == "Z" else offset
            if ":" not in iso_offset:
                iso_offset = f"{iso_offset[:3]}:{iso_offset[3:]}"
            moment = datetime.fromisoformat(moment.isoformat() + iso_offset)
            moment = moment.astimezone(DISPLAY_TIMEZONE)
    except ValueError:  # e.g. ExifTool's 0000:00:00 00:00:00
        return value
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def column_name(tag):
    return re.sub(r"\W", "_", tag)


COLUMNS = [column_name(tag) for tag in TAGS]
assert len(set(COLUMNS)) == len(COLUMNS), "tag column names must be unique"


def column_value(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _stored_value(tag, value):
    value = column_value(value)
    return format_datetime(value) if tag in DATE_TAGS else value


def column_values(metadata):
    """Return {column: value} for every tracked tag, None when absent."""
    return {
        column: _stored_value(tag, metadata[tag]) if tag in metadata else None
        for tag, column in zip(TAGS, COLUMNS)
    }


# ---------------------------------------------------------------------------------------
# When was this picture or video taken? Used to file it under a year and month.
#
# The best date available is used, in this order (the first that is a real date wins):
#   camera    the date the camera recorded (DateTimeOriginal and friends)
#   video     the creation date inside a video
#   modified  the date the picture was last edited (EXIF ModifyDate)
#   file      the date of the file on the disk (least reliable: a copy gets the day it was copied)
# Only the wall-clock time written in the file is used (no time-zone conversion), because a
# photo taken at 23:30 local time belongs to that day wherever the file is read.
# ---------------------------------------------------------------------------------------
TAKEN_SOURCES = (
    ("camera", (
        "ExifIFD:DateTimeOriginal",
        "Composite:SubSecDateTimeOriginal",
        "XMP-exif:DateTimeOriginal",
        "ExifIFD:CreateDate",
        "XMP-xmp:CreateDate",
        "XMP-photoshop:DateCreated",
    )),
    ("video", (
        "Keys:CreationDate",
        "QuickTime:CreationDate",
        "QuickTime:CreateDate",
        "QuickTime:MediaCreateDate",
        "QuickTime:TrackCreateDate",
    )),
    ("modified", ("IFD0:ModifyDate",)),
)
_TAKEN_PATTERN = re.compile(r"^\s*(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
EARLIEST_YEAR = 1905  # QuickTime's "zero" date is 1904; anything before 1905 is not a real date


def wall_clock(value, latest_year=None):
    """'YYYY-MM-DD HH:MM:SS' from an ExifTool date as written, or None if it is not a real date."""
    if not isinstance(value, str):
        return None
    match = _TAKEN_PATTERN.match(value)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    latest = latest_year or datetime.now().year + 1
    if not EARLIEST_YEAR <= year <= latest:
        return None
    try:
        datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"


def taken_date(metadata, file_modified=None):
    """(taken_at, source) for a file's metadata; source is camera, video, modified or file.
    (None, 'none') when no date is usable. file_modified is the file's own date as stored."""
    for source, tags in TAKEN_SOURCES:
        for tag in tags:
            found = wall_clock(metadata.get(tag))
            if found:
                return found, source
    found = wall_clock(file_modified)
    if found:
        return found, "file"
    return None, "none"
