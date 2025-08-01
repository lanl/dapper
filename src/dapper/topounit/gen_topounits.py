import ee
import geopandas as gpd
from dapper.utils import gee_utils as gu

def try_to_download_featurecollection(fc, verbose=True):
    """
    Attempts to directly download a featureColletion via the 
    python API. The ability to do this depends on GEE's willingness
    and the size of the features (i.e. number of vertices), so it
    is impossible to know a priori if it will fail or not. If 
    successful, this will return a GeoDataFrame with the features;
    else it returns None.
    """
    try:
        fc_geojson = fc.getInfo()  # May raise EEException
        gdf = gpd.GeoDataFrame.from_features(fc_geojson['features'])
        gdf.set_crs(epsg=4326, inplace=True)
        if verbose is True:
            print("Success! FeatureCollection loaded as GeoDataFrame.")
        return gdf

    except Exception as e:
        if verbose is True:
            print("Direct download failed. Reason:", e)
        
        return None 


def compute_sampling_scale(feature, total_topounits, target_pixels_per_topounit=500):
    """
    Computes an appropriate sampling scale (in meters) for DEM analysis,
    aiming for a target number of pixels per topounit to balance detail and performance.

    Parameters:
    - feature: ee.Feature or ee.Geometry
    - total_topounits: int, total number of topographic units to create
    - target_pixels_per_topounit: int, desired number of pixels per topounit
    - min_scale: int, minimum scale in meters (prevent overly detailed sampling)
    - max_scale: int, maximum scale in meters (prevent overly coarse sampling)

    Returns:
    - float: scale in meters
    """
    total_target_pixels = total_topounits * target_pixels_per_topounit

    # Ensure geometry
    geom = feature.geometry() if isinstance(feature, ee.Feature) else feature

    # Get area in square meters
    area_m2 = geom.area()

    # Required pixel size in m²
    required_pixel_area = area_m2.divide(total_target_pixels)

    # Scale is the square root of pixel area
    scale = required_pixel_area.sqrt().getInfo()
        
    return scale


def make_topounits(feature, 
                    method='epercentiles', 
                    max_topounits=11,
                    n_elev_bins=None, 
                    aspect_ranges=None, 
                    dem_source='arcticdem', 
                    return_as='gdf', # also 'asset'
                    export_scale='native', 
                    asset_name='topounits',
                    asset_ftype = 'GeoJSON',
                    verbose = False
                   ):
    """
    Creates topounits based on a provided set of geometries (polygons or multipolygons)
    already cast as an ee.Feature.
    """

    def make_epercentiles_masks(dem, thresholds):
        def make_band(i):
            low, high = thresholds[i], thresholds[i + 1]
            mask = dem.gte(low).And(dem.lt(high)).selfMask()
            return mask.rename(f"topounit_{i + 1}")
        bands = [make_band(i) for i in range(len(thresholds) - 1)]
        return add_epercentiles_metadata(ee.ImageCollection.fromImages(bands).toBands(), thresholds)

    def make_elevaspect_masks(dem, elev_thresholds, aspect_ranges):
        def create_aspect_classes():
            aspect_classes = []
            for start, end, code in aspect_ranges:
                mask = aspect_img.gte(start).And(aspect_img.lt(end)).selfMask() if start <= end else aspect_img.gte(start).Or(aspect_img.lt(end)).selfMask()
                aspect_classes.append((code, mask))
            return aspect_classes

        def create_bands():
            bands = []
            bin_id = 1
            for i in range(len(elev_thresholds) - 1):
                elev_mask = dem.gte(elev_thresholds[i]).And(dem.lt(elev_thresholds[i + 1])).selfMask()
                for code, asp_mask in aspect_classes:
                    bands.append(elev_mask.And(asp_mask).rename(f"topounit_{bin_id}"))
                    bin_id += 1
            return bands

        aspect_classes = create_aspect_classes()
        bands = create_bands()
        return add_elevaspect_metadata(ee.ImageCollection.fromImages(bands).toBands(), elev_thresholds, aspect_classes, aspect_ranges)

    def add_epercentiles_metadata(image, thresholds):
        metadata = [{'topounit_id': i + 1, 'min_elev': thresholds[i], 'max_elev': thresholds[i + 1]} for i in range(len(thresholds) - 1)]
        return image.set({'topounit_metadata': metadata})

    def add_elevaspect_metadata(image, elev_thresholds, aspect_classes, aspect_ranges):
        metadata, bin_id = [], 1
        for i in range(len(elev_thresholds) - 1):
            for j, (code, _) in enumerate(aspect_classes):
                start, end, _ = aspect_ranges[j]
                metadata.append({
                    'topounit_id': bin_id,
                    'min_elev': float(elev_thresholds[i]),
                    'max_elev': float(elev_thresholds[i + 1]),
                    'aspect_name': code,
                    'aspect_start': float(start),
                    'aspect_end': float(end)
                })
                bin_id += 1
        return image.set({'topounit_metadata': metadata})

    def masks_to_featurecollection(mask_image, region, scale):
        band_names = mask_image.bandNames().getInfo()
        metadata_list = mask_image.get('topounit_metadata').getInfo()
        features = []
        for name in band_names:
            band = mask_image.select(name)
            vectors = band.reduceToVectors(
                geometry=region,
                scale=scale,
                geometryType='polygon',
                eightConnected=False,
                bestEffort=True,
                maxPixels=1e13
            )
            geom = vectors.geometry()
            unit_id = int(name.split('_')[-1])
            meta = next(m for m in metadata_list if m['topounit_id'] == unit_id)
            features.append(ee.Feature(geom, meta))
        return ee.FeatureCollection(features)

    # Processing starts here
    # Handle generation method; check parameters don't overshoot max_topounits
    if method == 'epercentiles':
        if n_elev_bins is not None:
            total_topounits = n_elev_bins
        else:
            total_topounits = max_topounits
    elif method == 'elevaspect':
        if aspect_ranges is None:
            aspect_ranges = [(270, 90, 'N'), (90.01, 269.99, 'S')]
        num_aspect_bins = len(aspect_ranges)
        total_topounits = n_elev_bins * num_aspect_bins
        if total_topounits > max_topounits:
            raise ValueError(f"{total_topounits} topounits exceed max of {max_topounits}.")
    else:
        raise ValueError(f"Unsupported method: {method}")

    # Handle DEM loading and sampling scale
    if dem_source == 'arcticdem':
        dem = ee.Image("UMN/PGC/ArcticDEM/V4/2m_mosaic").select('elevation')
        scale_binning = max(compute_sampling_scale(feature, total_topounits), 5)
        if verbose is True:
            print(f'Using scale of {scale_binning} m for sampling elevations.')
    else:
        raise KeyError('DEM source not supported.')

    # Handle sampling
    dem_clipped = dem.clip(feature.geometry())
    aspect_img = ee.Terrain.aspect(dem_clipped)
    elev_samples = dem_clipped.sample(region=feature.geometry(), scale=scale_binning, geometries=False)
    elevations = elev_samples.aggregate_array('elevation')
    elevations_list = ee.List(elevations)
    count = elevations_list.size()
    percentiles = [i * (100 / n_elev_bins) for i in range(1, n_elev_bins)]
    thresholds = [elevations_list.sort().get(ee.Number(p).multiply(count).divide(100).int()) for p in percentiles]
    thresholds = ee.List(thresholds).getInfo()
    thresholds.insert(0, 0)
    thresholds.append(100000)
    
    # Generate masks
    if method == 'epercentiles':
        mask = make_epercentiles_masks(dem_clipped, thresholds) 
    elif method == 'elevaspect':
        mask = make_elevaspect_masks(dem_clipped, thresholds, aspect_ranges)

    if export_scale == 'native':
        export_scale = 2 if dem_source == 'arcticdem' else 100

    # Convert masks to polygons for export
    polygons_fc = masks_to_featurecollection(mask, feature.geometry(), export_scale)

    # Exporting
    gdf = None
    if return_as == 'gdf':
        gdf = try_to_download_featurecollection(polygons_fc, verbose)
        if gdf is None:
            print("Could not return as GeoDataFrame, exporting to Google Drive. Check Tasks in your GEE browser for completion." )
    
    if return_as== 'asset' or gdf is None:    
        gu.export_fc(polygons_fc, f'{asset_name}', asset_ftype, folder='topotest', verbose=True)
        return None

    return gdf