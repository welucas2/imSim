import galsim
import numpy as np
import dataclasses
from galsim.sensor import Sensor
from galsim.wcs import PixelScale

from imsim.stamp import ProcessingMode, StellarObject, build_obj


def merge_photon_arrays(arrays):
    """Given a list of photon arrays, return a single merged array.

    Parameters:
        arrays: list of galsim.PhotonArrays to be merged into one.

    Returns:
        merged: A single PhotonArray containing all the photons.
    """
    n_tot = sum(len(arr) for arr in arrays)
    merged = galsim.PhotonArray(n_tot)
    start = 0
    for arr in arrays:
        merged.assignAt(start, arr)
        start += len(arr)
    return merged


def accumulate_photons(photons, image, sensor, center):
    """Accumulate a photon array onto a sensor.

    Parameters:
        photons: A PhotonArray containing the photons to be accumulated.
        image: The image to which we draw the accumulated photons.
        sensor: Sensor to use for accumulation. If None, a temporary sensor is created here.
        center: Center of the image as galsim.PositionI.        
    """
    if sensor is None:
        sensor = Sensor()
    imview = image._view()
    imview._shift(-center)  # equiv. to setCenter(), but faster
    imview.wcs = PixelScale(1.0)
    photons.x -= 0.5
    photons.y -= 0.5
    if imview.dtype in (np.float32, np.float64):
        sensor.accumulate(photons, imview, imview.center)
    else:
        # Need a temporary
        im1 = galsim.image.ImageD(bounds=imview.bounds)
        sensor.accumulate(photons, im1, imview.center)
        imview += im1


def make_batches(objects, nbatch: int):
    """Generator converting an input list of objects to batches.

    Parameters:
        objects: List of objects to be yielded in batches.
        nbatch: The number of batches to create.

    Yields:
        A single batch made up of a list of object numbers.
    """
    per_batch = len(objects) // nbatch
    o_iter = iter(objects)
    for _ in range(nbatch):
        yield [obj for _, obj in zip(range(per_batch), o_iter)]


def build_stamps(base, logger, objects: list[StellarObject], stamp_type: str):
    """Create stamps for a list of StellarObjects.

    Parameters:
        base: The base configuration dictionary.
        logger: Logger object.
        objects: List of StellarObjects for which we will build stamps.
        stamp_type: The stamp type to be used.

    Returns:
        images: Tuple of the output stamps. In normal PhotonPooling usage these will actually be
            PhotonArrays to be processed into images after they have been pooled following this step.
        current_vars: Tuple of variables for each stamp (noise etc).
    """
    base["stamp"]["type"] = stamp_type
    if not objects:
        return [], []
    base["_objects"] = {obj.index: obj for obj in objects}

    images, current_vars = zip(
        *(
            galsim.config.BuildStamp(
                base, obj.index, xsize=0, ysize=0, do_noise=False, logger=logger
            )
            for obj in objects
        )
    )
    return images, current_vars


def make_photon_batches(config, base, logger, phot_objects: list[StellarObject], faint_objects: list[StellarObject], nbatch: int):
    """Create a set of nbatch batches of photon objects.
    The bright objects in phot_objects are replicated across all batches but at 1/nbatch their original
    flux, while the faint objects in faint_objects are randomly placed in batches at full flux.

    Parameters:
        config: The configuration dictionary for the image field.
        base: The base configuration dictionary.
        logger: Logger to record progress.
        phot_objects: The list of StellarObjects representing the bright photon objects to be batched.
        faint_objects: The list of StellarObjects representing faint photon objects to be batched.
        nbatch: The integer number of photon batches to create.

    Returns:
        batches: A list of batches, each itself a list of the objects to be drawn.
    """
    if not phot_objects and not faint_objects:
        return []
    # Each batch is a copy of the original list of objects at 1/nbatch the original flux.
    batches = [
        [dataclasses.replace(obj, phot_flux=np.floor(obj.phot_flux / nbatch)) for obj in phot_objects]
    for _ in range(nbatch)]
    rng = galsim.config.GetRNG(config, base, logger, "LSST_Silicon")
    ud = galsim.UniformDeviate(rng)
    # Shuffle faint objects into the batches randomly:
    for obj in faint_objects:
        batch_index = int(ud() * nbatch)
        batches[batch_index].append(obj)
    return batches


def stamp_bounds(stamp, full_image_bounds):
    """Check bounds overlap between an object's stamp and the full image.

    Parameters:
        stamp: An object's stamp, potentially None.
        full_image_bounds: The full image's galsim.BoundsI.
    Returns: 
        bounds: The overlapping bounds
        or None if the stamp and image do not overlap
        or None if the object was not drawn (i.e. does not have a stamp).
    """
    if stamp is None:
        return None
    bounds = stamp.bounds & full_image_bounds
    if not bounds.isDefined():  # pragma: no cover
        # These normally show up as stamp==None, but technically it is possible
        # to get a stamp that is off the main image, so check for that here to
        # avoid an error.  But this isn't covered in the imsim test suite.
        return None
    return bounds


def partition_objects(objects):
    """Given a list of objects, return three lists containing only the objects to
    be processed as FFT, photon or faint objects.

    Parameters:
        objects: a list of StellarObjects

    Returns:
        A tuple of three lists respectively containing the objects to be processed
        with FFTs, photons and as faint photon objects.
    """
    objects_by_mode = {
        ProcessingMode.FFT: [],
        ProcessingMode.PHOT: [],
        ProcessingMode.FAINT: [],
    }
    for obj in objects:
        objects_by_mode[obj.mode].append(obj)
    return (
        objects_by_mode[ProcessingMode.FFT],
        objects_by_mode[ProcessingMode.PHOT],
        objects_by_mode[ProcessingMode.FAINT],
    )


def load_objects(obj_numbers, config, base, logger):
    """Convert the objects in the base configuration to StellarObjects. Their
    fluxes are calculated at this stage and then stored in the StellarObjects
    for reuse later on.

    Parameters:
        obj_numbers: a list of the object numbers in the config that are to be drawn.
        config: The configuration dictionary for the image field.
        base: The base configuration dictionary.
        logger: A Logger object to track progress.
    
    Yields:
        obj: A StellarObject caching an object to be drawn and its flux.
    """
    gsparams = {}
    stamp = base['stamp']
    if 'gsparams' in stamp:
        gsparams = galsim.gsobject.UpdateGSParams(gsparams, stamp['gsparams'], config)

    for obj_num in obj_numbers:
        galsim.config.SetupConfigObjNum(base, obj_num, logger)
        obj = build_obj(stamp, base, logger)
        if obj is not None:
            yield obj


def create_full_image(config, base):
    """Create the GalSim image on which we will place the individual
    object stamps once they are drawn.

    Parameters:
        config: The configuration dictionary for the image field.
        base: The base configuration dictionary.

    Returns:
        full_image: The galsim.Image representing the full field.
    """
    if galsim.__version_info__ < (2,5):
        # GalSim 2.4 required a bit more work here.
        from galsim.config.stamp import _ParseDType

        full_xsize = base['image_xsize']
        full_ysize = base['image_ysize']
        wcs = base['wcs']

        dtype = _ParseDType(config, base)

        full_image = galsim.Image(full_xsize, full_ysize, dtype=dtype)
        full_image.setOrigin(base['image_origin'])
        full_image.wcs = wcs
        full_image.setZero()
        base['current_image'] = full_image
    else:
        # In GalSim 2.5+, the image is already built and available as 'current_image'
        full_image = base['current_image']
    return full_image


def set_config_image_pos(config, base):
    """Determine the image position if necessary using information
    from the base configuration.

    Parameters:
        config: The configuration dictionary for the image field.
        base: The base configuration dictionary.
    """

    if 'image_pos' in config and 'world_pos' in config:
        raise galsim.config.GalSimConfigValueError(
            "Both image_pos and world_pos specified for LSST_Image.",
            (config['image_pos'], config['world_pos']))

    if ('image_pos' not in config and 'world_pos' not in config and
            not ('stamp' in base and
                ('image_pos' in base['stamp'] or 'world_pos' in base['stamp']))):
        full_xsize = base['image_xsize']
        full_ysize = base['image_ysize']
        xmin = base['image_origin'].x
        xmax = xmin + full_xsize-1
        ymin = base['image_origin'].y
        ymax = ymin + full_ysize-1
        config['image_pos'] = {
            'type' : 'XY' ,
            'x' : { 'type' : 'Random' , 'min' : xmin , 'max' : xmax },
            'y' : { 'type' : 'Random' , 'min' : ymin , 'max' : ymax }
        }


def load_checkpoint(checkpoint, chk_name, base, logger):
    """Load a checkpoint from file.

    Parameters:
        checkpoint: A Checkpointer object.
        chk_name: The checkpoint record's name.
        base: The base configuration dictionary.
        logger: A Logger to provide information.

    Returns:
        full_image: The full image as saved to checkpoint, or None.
        all_vars: List of variables e.g. noise levels, or [].
        all_stamps: List of stamps created as of the time of the checkpoint, or [].
        all_obj_nums: List of object IDs drawn as of the time of the checkpoint, or [].
        current_photon_batch_num: The photon batch from which to start working, or 0.
    """
    saved = checkpoint.load(chk_name)
    if saved is not None:
        # If the checkpoint exists, get the stored information and prepare it for use.
        full_image, all_bounds, all_vars, all_obj_nums, extra_builder, current_photon_batch_num = saved
        if extra_builder is not None:
            base['extra_builder'] = extra_builder
        # Create stamps from the bounds provided by the checkpoint.
        all_stamps = [galsim._Image(np.array([]), b, full_image.wcs) for b in all_bounds]
        logger.warning('File %d: Loaded checkpoint data from %s.',
                       base.get('file_num', 0), checkpoint.file_name)
        return full_image, all_vars, all_stamps, all_obj_nums, current_photon_batch_num
    else:
        # Return empty objects if the checkpoint doesn't yet exist.
        return None, [], [], [], 0


def save_checkpoint(checkpoint, chk_name, base, full_image, all_stamps, all_vars, all_obj_nums, current_photon_batch_num):
    """Save a checkpoint to file.

    Parameters:
        checkpoint: A Checkpointer object.
        chk_name: The record name with which to save the checkpoint.
        base: The base configuration dictionary.
        full_image: The current state of the GalSim image containing the full field.
        all_stamps: List of the stamps drawn so far -- note that only their bounds are saved.
        all_vars: List of variables e.g. noise levels.
        all_obj_nums: List of the objects which have been drawn so far. 
        current_photon_batch_num: The photon batch number from which drawing should begin
            if this checkpoint is loaded.
    """
    # Don't save the full stamps.  All we need for FlattenNoiseVariance is the bounds.
    # Everything else about the stamps has already been handled above.
    all_bounds = [stamp.bounds for stamp in all_stamps]
    data = (full_image, all_bounds, all_vars, all_obj_nums,
            base.get('extra_builder',None), current_photon_batch_num)
    checkpoint.save(chk_name, data)