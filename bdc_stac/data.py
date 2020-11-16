"""Data module."""
import json
import warnings
from copy import deepcopy
from datetime import datetime as dt
from functools import lru_cache

from bdc_catalog.models import Band, Collection, CompositeFunction, GridRefSys, Item, Tile
from bdc_catalog.models.base_sql import db
from flask_sqlalchemy import SQLAlchemy
from geoalchemy2.functions import GenericFunction
from sqlalchemy import Float, and_, cast, exc, func, or_
from .config import BDC_STAC_API_VERSION, BDC_STAC_FILE_ROOT, BDC_STAC_MAX_LIMIT

with warnings.catch_warnings():
    warnings.simplefilter("ignore", category=exc.SAWarning)


db = SQLAlchemy()

session = db.create_scoped_session({"autocommit": True})


class ST_Extent(GenericFunction):
    """Postgis ST_Extent function."""

    name = "ST_Extent"
    type = None


def get_collection_items(
    collection_id=None,
    roles=[],
    item_id=None,
    bbox=None,
    datetime=None,
    ids=None,
    collections=None,
    intersects=None,
    page=1,
    limit=10,
    query=None,
    **kwargs,
):
    """Retrieve a list of collection items based on filters.

    :param collection_id: Single Collection ID to include in the search for items.
                          Only Items in one of the provided Collection will be searched, defaults to None
    :type collection_id: str, optional
    :param item_id: item identifier, defaults to None
    :type item_id: str, optional
    :param bbox: bounding box for intersection [west, north, east, south], defaults to None
    :type bbox: list, optional
    :param datetime: Single date+time, or a range ("/" seperator), formatted to RFC 3339, section 5.6, defaults to None
    :type datetime: str, optional
    :param ids: Array of Item ids to return. All other filter parameters that further restrict the
                number of search results are ignored, defaults to None
    :type ids: list, optional
    :param collections: Array of Collection IDs to include in the search for items.
                        Only Items in one of the provided Collections will be searched, defaults to None
    :type collections: list, optional
    :param intersects: Searches items by performing intersection between their geometry and provided GeoJSON geometry.
                       All GeoJSON geometry types must be supported., defaults to None
    :type intersects: dict, optional
    :param page: The page offset of results, defaults to 1
    :type page: int, optional
    :param limit: The maximum number of results to return (page size), defaults to 10
    :type limit: int, optional
    :return: list of collectio items
    :rtype: list
    """
    columns = [
        func.concat(Collection.name, "-", Collection.version).label("collection"),
        Collection.collection_type,
        Collection._metadata.label("meta"),
        Item.name.label("item"),
        Item.collection_id,
        Item.start_date.label("start"),
        Item.end_date.label("end"),
        Item.assets,
        Item.created,
        Item.updated,
        cast(Item.cloud_cover, Float).label("cloud_cover"),
        func.ST_AsGeoJSON(Item.geom).label("geom"),
        func.Box2D(Item.geom).label("bbox"),
        Tile.name.label("tile"),
    ]

    where = [
        Collection.id == Item.collection_id,
        or_(Collection.is_public.is_(True), Collection.id.in_([int(r.split(":")[0]) for r in roles])),
    ]

    if ids is not None:
        where += [Item.name.in_(ids.split(","))]
    elif item_id is not None:
        where += [Item.name.like(item_id)]
    else:
        if collections is not None:
            where += [func.concat(Collection.name, "-", Collection.version).in_(collections.split(","))]
        elif collection_id is not None:
            where += [func.concat(Collection.name, "-", Collection.version) == collection_id]

        if query:
            filters = create_query_filter(query)
            if filters:
                where += filters

        if intersects is not None:
            where += [func.ST_Intersects(func.ST_GeomFromGeoJSON(str(intersects)), Item.geom)]
        elif bbox is not None:
            try:
                split_bbox = [float(x) for x in bbox.split(",")]
                if split_bbox[0] == split_bbox[2] or split_bbox[1] == split_bbox[3]:
                    raise InvalidBoundingBoxError("")

                where += [
                    func.ST_Intersects(
                        func.ST_MakeEnvelope(
                            split_bbox[0], split_bbox[1], split_bbox[2], split_bbox[3], func.ST_SRID(Item.geom),
                        ),
                        Item.geom,
                    )
                ]
            except:
                raise (InvalidBoundingBoxError(f"'{bbox}' is not a valid bbox."))

        if datetime is not None:
            date_filter = None
            if "/" in datetime:
                time_start, time_end = datetime.split("/")
                date_filter = [
                    or_(
                        and_(Item.start_date >= time_start, Item.start_date <= time_end),
                        and_(Item.end_date >= time_start, Item.end_date <= time_end),
                    )
                ]
            else:
                date_filter = [or_(Item.start_date <= datetime, Item.end_date <= datetime)]

            where += date_filter
    outer = [Item.tile_id == Tile.id]
    query = session.query(*columns).outerjoin(Tile, *outer).filter(*where).order_by(Item.start_date.desc())

    result = query.paginate(page=int(page), per_page=int(limit), error_out=False, max_per_page=BDC_STAC_MAX_LIMIT)

    return result


@lru_cache()
def get_collection_eo(collection_id):
    """Get Collection Eletro-Optical properties.

    Args:
        collection_id (str): collection identifier
    Returns:
        eo_gsd, eo_bands (tuple(float, dict)):
    """
    bands = (
        session.query(
            Band.name,
            Band.common_name,
            Band.description,
            cast(Band.min_value, Float).label("min"),
            cast(Band.max_value, Float).label("max"),
            cast(Band.nodata, Float).label("nodata"),
            cast(Band.scale, Float).label("scale"),
            cast(Band.resolution_x, Float).label("gsd"),
            Band.data_type,
            cast(Band.center_wavelength, Float).label("center_wavelength"),
            cast(Band.full_width_half_max, Float).label("full_width_half_max"),
        )
        .filter(Band.collection_id == collection_id)
        .all()
    )
    eo_bands = list()
    eo_gsd = 0.0

    for band in bands:
        eo_bands.append(
            dict(
                name=band.name,
                common_name=band.common_name,
                description=band.description,
                min=band.min,
                max=band.max,
                nodata=band.nodata,
                scale=band.scale,
                center_wavelength=band.center_wavelength,
                full_width_half_max=band.full_width_half_max,
                data_type=band.data_type,
            )
        )
        if band.gsd > eo_gsd:
            eo_gsd = band.gsd

    return {"eo:gsd": eo_gsd, "eo:bands": eo_bands}


def get_collection_bands(collection_id):
    """Retrive a dict of bands for a given collection.

    :param collection_id: collection identifier
    :type collection_id: str
    :return: dict of bands for the collection
    :rtype: dict
    """
    bands = (
        session.query(
            Band.name,
            Band.common_name,
            cast(Band.min, Float).label("min"),
            cast(Band.max, Float).label("max"),
            cast(Band.nodata, Float).label("nodata"),
            cast(Band.scale, Float).label("scale"),
            Band.data_type,
        )
        .filter(Band.collection_id == collection_id)
        .all()
    )
    bands_json = dict()

    for b in bands:
        bands_json[b.common_name] = {
            k: v for k, v in b._asdict().items() if k != "common_name" and not k.startswith("_")
        }

    return bands_json


def get_collection_tiles(collection_id):
    """Retrive a list of tiles for a given collection.

    :param collection_id: collection identifier
    :type collection_id: str
    :return: list of tiles for the collection
    :rtype: list
    """
    tiles = (
        session.query(Tile.name)
        .filter(Item.collection_id == collection_id, Item.tile_id == Tile.id)
        .group_by(Tile.name)
        .all()
    )

    return [t.name for t in tiles]


@lru_cache()
def get_collection_crs(collection_id):
    """Retrive the CRS for a given collection.

    :param collection_id: collection identifier
    :type collection_id: str
    :return: CRS for the collection
    :rtype: str
    """
    grs = (
        session.query(GridRefSys)
        .filter(Collection.id == collection_id, Collection.grid_ref_sys_id == GridRefSys.id)
        .first()
    )

    return grs.crs


def get_collection_timeline(collection_id):
    """Retrive a list of dates for a given collection.

    :param collection_id: collection identifier
    :type collection_id: str
    :return: list of dates for the collection
    :rtype: list
    """
    timeline = (
        session.query(Item.start_date)
        .filter(Item.collection_id == collection_id)
        .group_by(Item.start_date)
        .order_by(Item.start_date.asc())
        .all()
    )

    return [dt.fromisoformat(str(t.start_date)).strftime("%Y-%m-%d") for t in timeline]


def get_collection_extent(collection_id):
    """Retrive the extent as a BBOX for a given collection.

    :param collection_id: collection identifier
    :type collection_id: str
    :return: list of coordinates for the collection extent
    :rtype: list
    """
    extent = (
        session.query(func.ST_Extent(Item.geom).label("bbox"))
        .filter(Collection.id == Item.collection_id, Collection.id == collection_id)
        .first()
    )

    bbox = list()
    if extent.bbox:
        bbox = extent.bbox[extent.bbox.find("(") + 1 : extent.bbox.find(")")].replace(" ", ",")
        bbox = [float(coord) for coord in bbox.split(",")]
    return bbox


def get_collection_quicklook(collection_id):
    """Retrive a list of bands used to create the quicklooks for a given collection.

    :param collection_id: collection identifier
    :type collection_id: str
    :return: list of bands
    :rtype: list.
    """
    quicklook_bands = session.execute(
        "SELECT  array[r.name, g.name, b.name] as quicklooks "
        "FROM bdc.quicklook q "
        "INNER JOIN bdc.bands r ON q.red = r.id "
        "INNER JOIN bdc.bands g ON q.green = g.id "
        "INNER JOIN bdc.bands b ON q.blue = b.id "
        "INNER JOIN bdc.collections c ON q.collection_id = c.id "
        "WHERE c.id = :collection_id",
        {"collection_id": collection_id},
    ).fetchone()

    return quicklook_bands["quicklooks"] if quicklook_bands else None


def get_collections(collection_id=None, roles=[]):
    """Retrieve information of all collections or one if an id is given.

    :param collection_id: collection identifier
    :type collection_id: str
    :return: list of collections
    :rtype: list
    """
    columns = [
        Collection.id,
        Collection.is_public,
        Collection.start_date.label("start"),
        Collection.end_date.label("end"),
        Collection.description,
        Collection._metadata.label("meta"),
        func.concat(Collection.name, "-", Collection.version).label("name"),
        Collection.collection_type,
        Collection.version,
        Collection.title,
        Collection.temporal_composition_schema,
        CompositeFunction.name.label("composite_function"),
        GridRefSys.name.label("grid_ref_sys"),
    ]

    where = [
        or_(Collection.is_public.is_(True), Collection.id.in_([int(r.split(":")[0]) for r in roles])),
    ]

    if collection_id:
        where.append(func.concat(Collection.name, "-", Collection.version) == collection_id)

    result = (
        session.query(*columns)
        .outerjoin(CompositeFunction, Collection.composite_function_id == CompositeFunction.id)
        .outerjoin(GridRefSys, Collection.grid_ref_sys_id == GridRefSys.id)
        .filter(*where)
        .all()
    )

    collections = list()

    for r in result:
        collection = dict()
        collection["id"] = r.name

        collection["stac_version"] = BDC_STAC_API_VERSION
        collection["stac_extensions"] = ["commons", "datacube", "version"]
        collection["title"] = r.title
        collection["version"] = r.version
        collection["deprecated"] = False
        collection["description"] = r.description

        if r.meta and ("rightsList" in r.meta) and (len(r.meta["rightsList"]) > 0):

            collection["license"] = r.meta["rightsList"][0].get("rights", "")
        else:
            collection["license"] = ""

        collection["properties"] = dict()

        bbox = get_collection_extent(r.id)

        start, end = None, None

        if r.start:
            start = r.start.strftime("%Y-%m-%dT%H:%M:%S")
            if r.end:
                end = r.end.strftime("%Y-%m-%dT%H:%M:%S")

        collection["extent"] = {
            "spatial": {"bbox": [bbox]},
            "temporal": {"interval": [[start, end]]},
        }

        quicklooks = get_collection_quicklook(r.id)

        if quicklooks is not None:
            collection["bdc:bands_quicklook"] = quicklooks

        collection_eo = get_collection_eo(r.id)
        collection["properties"].update(collection_eo)

        if r.meta:
            if "platform" in r.meta:
                collection["properties"]["instruments"] = r.meta["platform"]["instruments"]
                collection["properties"]["platform"] = r.meta["platform"]["code"]

                r.meta.pop("platform") # platform info is displayed on properties
            collection["bdc:metadata"] = r.meta

        collection["bdc:grs"] = r.grid_ref_sys
        collection["bdc:tiles"] = get_collection_tiles(r.id)
        collection["bdc:composite_function"] = r.composite_function


        if r.collection_type == "cube":
            proj4text = get_collection_crs(r.id)

            datacube = dict()
            datacube["x"] = dict(type="spatial", axis="x", extent=[bbox[0], bbox[2]], reference_system=proj4text)
            datacube["y"] = dict(type="spatial", axis="y", extent=[bbox[1], bbox[3]], reference_system=proj4text)
            datacube["temporal"] = dict(type="temporal", extent=[start, end], values=get_collection_timeline(r.id))

            datacube["bands"] = dict(type="bands", values=[band["name"] for band in collection_eo["eo:bands"]])

            collection["cube:dimensions"] = datacube
            collection["bdc:crs"] = get_collection_crs(r.id)
            collection["bdc:temporal_composition"] = r.temporal_composition_schema

        collections.append(collection)

    return collections


def get_catalog(roles=[]):
    """Retrive all available collections.

    :return: a list of available collections
    :rtype: list
    """
    collections = (
        session.query(
            Collection.id, func.concat(Collection.name, "-", Collection.version).label("name"), Collection.title,
        )
        .filter(or_(Collection.is_public.is_(True), Collection.id.in_([int(r.split(":")[0]) for r in roles]),))
        .all()
    )
    return collections


def make_geojson(items, links, access_token=""):
    """Generate a list of STAC Items from a list of collection items.

    :param items: collection items to be formated as GeoJSON Features
    :type items: list
    :param links: links for STAC navigation
    :type links: list
    :return: GeoJSON Features.
    :rtype: list
    """
    features = list()

    for i in items:
        feature = dict()

        feature["type"] = "Feature"
        feature["id"] = i.item
        feature["collection"] = i.collection
        feature["stac_version"] = BDC_STAC_API_VERSION
        feature["stac_extensions"] = ["checksum", "commons", "eo"]

        feature["geometry"] = json.loads(i.geom)

        bbox = list()
        if i.bbox:
            bbox = i.bbox[i.bbox.find("(") + 1 : i.bbox.find(")")].replace(" ", ",")
            bbox = [float(coord) for coord in bbox.split(",")]
        feature["bbox"] = bbox

        bands = get_collection_eo(i.collection_id)

        properties = dict()
        start = dt.fromisoformat(str(i.start)).strftime("%Y-%m-%dT%H:%M:%S")
        properties["bdc:tile"] = i.tile
        properties["datetime"] = start

        if i.collection_type == "cube" and i.start != i.end:
            properties["start_datetime"] = start
            properties["end_datetime"] = dt.fromisoformat(str(i.end)).strftime("%Y-%m-%dT%H:%M:%S")

        properties["created"] = i.created.strftime("%Y-%m-%dT%H:%M:%S")
        properties["updated"] = i.updated.strftime("%Y-%m-%dT%H:%M:%S")
        properties.update(bands)
        properties["eo:cloud_cover"] = i.cloud_cover

        for key, value in i.assets.items():
            value["href"] = BDC_STAC_FILE_ROOT + value["href"] + access_token
            for index, band in enumerate(properties["eo:bands"], start=0):
                if band["name"] == key:
                    value["eo:bands"] = [index]

        if i.meta:
            if "platform" in i.meta:
                properties["instruments"] = i.meta["platform"]["instruments"]
                properties["platform"] = i.meta["platform"]["code"]

                i.meta.pop("platform") # platform info is displayed on properties
            properties["bdc:metadata"] = i.meta

        feature["properties"] = properties
        feature["assets"] = i.assets

        feature["links"] = deepcopy(links)
        feature["links"][0]["href"] += i.collection + "/items/" + i.item + access_token
        feature["links"][1]["href"] += i.collection + access_token
        feature["links"][2]["href"] += i.collection + access_token

        features.append(feature)

    return features


def create_query_filter(query):
    """Create STAC query filter for SQLAlchemy.

    Notes:
        Queryable properties must be mapped in this functions.
    """
    mapping = {
        "eq": "__eq__",
        "neq": "__ne__",
        "lt": "__lt__",
        "lte": "__le__",
        "gt": "__gt__",
        "gte": "__ge__",
        "startsWith": "startswith",
        "endsWith": "endswith",
        "contains": "contains",
        "in": "in_",
    }

    bdc_properties = {
        "bdc:tile": Tile.name,
        "eo:cloud_cover": Item.cloud_cover,
    }

    filters = []

    for column, _filters in query.items():
        for op, value in _filters.items():
            f = getattr(bdc_properties[column], mapping[op])(value)
            filters.append(f)

    return filters if len(filters) > 0 else None


class InvalidBoundingBoxError(Exception):
    """Exception for malformed bounding box."""

    def __init__(self, description):
        """Initialize exception with a description.

        :param description: exception description.
        :type description: str
        """
        super(InvalidBoundingBoxError, self).__init__()
        self.description = description

    def __str__(self):
        """:return: str representation of the exception."""
        return str(self.description)
