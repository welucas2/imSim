import os
import copy
import numpy as np
import scipy
from collections import namedtuple
from astropy.io import fits
from astropy.time import Time
import galsim
from galsim.config import ExtraOutputBuilder, RegisterExtraOutput
from lsst.afw import cameraGeom
import lsst.obs.lsst
from .bleed_trails import bleed_eimage
from .camera import Camera, get_camera
from .batoid_wcs import BatoidWCSBuilder
from ._version import __version__


_rotSkyPos_cache = {}

def make_batoid_wcs(ra0, dec0, rottelpos, obsmjd, band, camera_name,
                    logger=None):
    """
    Create a WCS object from Opsim db parameters for the center
    science CCD.

    Parameters
    ----------
    ra0 : float
        RA of boresight direction in degrees.
    dec0 : float
        Dec of boresight direction in degrees.
    rottelpos : float
        Angle of the telescope rotator with respect to the mount in degrees.
    obsmjd : float
        MJD of the observation.
    band : str
        One of `ugrizy`.
    camera_name : str ['LsstCam']
        Class name of the camera to be simulated.  Valid values are
        'LsstCam', 'LsstComCam', 'LsstCamImSim'.
    logger : logger.Logger [None]
        Logger object.

    Returns
    -------
    galsim.GSFitsWCS
    """
    if band not in 'ugrizy':
        if logger is not None:
            logger.info(f'Requested band is "{band}.  Setting it to "r"')
        band = 'r'
    obstime = Time(obsmjd, format='mjd')
    boresight = galsim.CelestialCoord(ra0*galsim.degrees, dec0*galsim.degrees)
    factory = BatoidWCSBuilder().makeWCSFactory(
        boresight, rottelpos*galsim.degrees, obstime, band, camera=camera_name)

    # Use the science sensor at the center of the focal plane.
    camera = get_camera(camera_name)
    detectors = [i for i, det in enumerate(camera)
                 if det.getType() == cameraGeom.DetectorType.SCIENCE]
    det = camera[int(np.median(detectors))]
    return factory.getWCS(det)


def compute_rotSkyPos(ra0, dec0, rottelpos, obsmjd, band,
                      camera_name='LsstCam', dxy=100, pixel_scale=0.2,
                      logger=None):
    """
    Compute the nominal rotation angle of the focal plane wrt
    Celestial North using the +y direction in pixel coordinates as the
    reference direction for the focal plane.

    Parameters
    ----------
    ra0 : float
        RA of boresight direction in degrees.
    dec0 : float
        Dec of boresight direction in degrees.
    rottelpos : float
        Angle of the telescope rotator with respect to the mount in degrees.
    obsmjd : float
        MJD of the observation.
    band : str
        One of `ugrizy`.
    camera_name : str ['LsstCam']
        Class name of the camera to be simulated.  Valid values are
        'LsstCam', 'LsstComCam', 'LsstCamImSim'.
    dxy : float [100]
        Size (in pixels) of legs of the triangle to use for computing the
        angle between North and the +y direction in the focal plane.
    pixel_scale : float [0.2]
        Pixel scale in arcsec.
    logger : logger.Logger [None]
        Logger object.

    Returns
    -------
    float  The rotSkyPos angle in degrees.
    """
    args = ra0, dec0, rottelpos, obsmjd, band, camera_name
    if args in _rotSkyPos_cache:
        return _rotSkyPos_cache[args]

    wcs = make_batoid_wcs(ra0, dec0, rottelpos, obsmjd, band, camera_name)

    # CCD center
    x0, y0 = wcs.crpix
    # Offset position towards top of CCD.
    x1, y1 = x0, y0 + dxy
    # Offset position towards Celestial North.
    ra = wcs.center.ra
    dec = wcs.center.dec + pixel_scale*dxy/3600.*galsim.degrees
    pos = wcs.toImage(galsim.CelestialCoord(ra, dec))
    x2, y2 = pos.x, pos.y
    # Use law of cosines to find rotskypos:
    a2 = (x1 - x0)**2 + (y1 - y0)**2
    b2 = (x2 - x0)**2 + (y2 - y0)**2
    c2 = (x1 - x2)**2 + (y2 - y1)**2
    cos_theta = (a2 + b2 - c2)/2./np.sqrt(a2*b2)

    theta = np.degrees(np.arccos(cos_theta))
    # Define angle between focal plane y-axis and North as positive
    # if North is counter-clockwise from y-axis.
    if x2 < x1:
        theta = 360 - theta

    if camera_name == 'LsstCamImSim':
        # For historical reasons, the rotation angle for imSim data is
        # assumed by the LSST code to have a sign change and 90
        # rotation.  See
        # https://github.com/lsst/obs_lsst/blob/main/python/lsst/obs/lsst/translators/imsim.py#L104
        theta = 90 - theta
    if theta < 0:
        theta += 360
    _rotSkyPos_cache[args] = theta
    return theta


def section_keyword(bounds, flipx=False, flipy=False):
    """Package image bounds as a NOAO image section keyword value."""
    xmin, xmax = bounds.xmin, bounds.xmax
    ymin, ymax = bounds.ymin, bounds.ymax
    if flipx:
        xmin, xmax = xmax, xmin
    if flipy:
        ymin, ymax = ymax, ymin
    return '[%i:%i,%i:%i]' % (xmin, xmax, ymin, ymax)


def cte_matrix(npix, cti, ntransfers=20):
    """
    Compute the CTE matrix so that the apparent charge q_i in the i-th
    pixel is given by
    q_i = Sum_j cte_matrix_ij q0_j
    where q0_j is the initial charge in j-th pixel.  The corresponding
    python code would be
    >>> cte = cte_matrix(npix, cti)
    >>> qout = numpy.dot(cte, qin)
    Parameters
    ----------
    npix : int
        Total number of pixels in either the serial or parallel
        directions.
    cti : float
        The charge transfer inefficiency, i.e., the fraction of
        a pixel's charge left behind for each single pixel transfer.
    ntransfers : int [20]
        Maximum number of transfers to consider as contributing to
        a target pixel.
    Returns
    -------
    numpy.array
        The npix x npix numpy array containing the CTE matrix.

    This implementation is from Mike Jarvis.
    """
    my_matrix = np.zeros((npix, npix), dtype=float)
    for i in range(1, npix+1):
        # On diagonal, there are i transfers of the electrons, so i chances to lose a fraction
        # cti into the later pixels.  Net charge is decreased by (1-cti)**i.
        my_matrix[i-1, i-1] = (1.-cti)**i

        # Off diagonal, there must be (i-j) cti losses of charge among i-1 possible transfers.
        # Then that charge has to survive j additional transfers.
        # So net charge is binom(i-1,i-j) * (1-cti)**j * cti**(i-j)
        # (Indeed this is the same equation as above when i=j, but slightly more efficient to
        # break it out separately above.)
        jmin = max(1, i-ntransfers)
        j = np.arange(jmin, i)
        my_matrix[i-1, jmin-1:i-1] = scipy.special.binom(i-1, i-j) * (1.-cti)**j * cti**(i-j)

    return my_matrix


# TODO: get lsst_num from camera object.
def get_primary_hdu(eimage, lsst_num='LCA-11021_RTM-000', image_type=None,
                    added_keywords={}, logger=None):
    """
    Create a primary HDU for the output raw file with the keywords
    needed to process with the LSST Stack.
    """
    phdu = fits.PrimaryHDU()
    phdu.header['RUNNUM'] = eimage.header['OBSID']
    phdu.header['OBSID'] = eimage.header['OBSID']
    phdu.header['MJD'] = eimage.header['MJD']
    date = Time(eimage.header['MJD'], format='mjd')
    phdu.header['DATE'] = date.isot
    phdu.header['DAYOBS'] = date.strftime('%Y%m%d')
    phdu.header['SEQNUM'] = eimage.header['SEQNUM']
    exptime = eimage.header['EXPTIME']
    phdu.header['EXPTIME'] = exptime
    phdu.header['DARKTIME'] = exptime
    phdu.header['TIMESYS'] = 'TAI'
    phdu.header['LSST_NUM'] = lsst_num
    phdu.header['IMGTYPE'] = eimage.header['IMGTYPE']
    phdu.header['OBSTYPE'] = eimage.header['IMGTYPE']
    phdu.header['MONOWL'] = -1
    det_name = eimage.header['DET_NAME']
    raft, sensor = det_name.split('_')
    camera_name = eimage.header['CAMERA']
    ratel = eimage.header['RATEL']
    dectel = eimage.header['DECTEL']
    rottelpos = eimage.header['ROTTELPOS']
    band = eimage.header['FILTER']
    if camera_name == 'LsstCamImSim':
        phdu.header['TESTTYPE'] = 'IMSIM'
        phdu.header['RAFTNAME'] = raft
        phdu.header['SENSNAME'] = sensor
        phdu.header['RATEL'] = ratel
        phdu.header['DECTEL'] = dectel
    else:
        phdu.header['INSTRUME'] = 'LSSTCam'
        phdu.header['RAFTBAY'] = raft
        phdu.header['CCDSLOT'] = sensor
        phdu.header['RA'] = ratel
        phdu.header['DEC'] = dectel
        phdu.header['ROTCOORD'] = 'sky'
    # Compute rotSkyPos instead of using likely inconsistent values
    # from the instance catalog or opsim db.
    mjd_obs = eimage.header['MJD-OBS']
    mjd_end =  mjd_obs + exptime/86400.
    phdu.header['ROTANGLE'] = compute_rotSkyPos(
        ratel, dectel, rottelpos, mjd_obs, band, camera_name=camera_name,
        logger=logger)
    phdu.header['MJD-OBS'] = mjd_obs
    phdu.header['FILTER'] = band
    phdu.header['HASTART'] = eimage.header['HASTART']
    phdu.header['HAEND'] = eimage.header['HAEND']
    phdu.header['DATE-OBS'] = Time(mjd_obs, format='mjd', scale='tai').to_value('isot')
    phdu.header['DATE-END'] = Time(mjd_end, format='mjd', scale='tai').to_value('isot')
    phdu.header['AMSTART'] = eimage.header['AMSTART']
    phdu.header['AMEND'] = eimage.header['AMEND']
    phdu.header['IMSIMVER'] = __version__
    phdu.header['PKG00000'] = 'throughputs'
    phdu.header['VER00000'] = '1.4'
    phdu.header['CHIPID'] = det_name

    phdu.header.update(added_keywords)
    return phdu


class CcdReadout:
    def __init__(self, eimage, logger, ccd_params=None):
        self.eimage = eimage
        self.det_name = eimage.header['DET_NAME']
        self.camera_name = eimage.header['CAMERA']
        self.logger = logger
        camera = Camera(self.camera_name)
        self.ccd = camera[self.det_name]
        if ccd_params is None:
            ccd_params = {}
        self.exp_time = ccd_params.get('exp_time', 30.0)  # seconds
        self.readout_time = ccd_params.get('readout_time', 2.0)  # seconds
        self.dark_current = ccd_params.get('dark_current', 0.02)  # e-/s
        self.bias_level = ccd_params.get('bias_level', 1000.0)  # ADU
        scti = ccd_params.get('scti', 1.0e-6)  # serial CTI
        pcti = ccd_params.get('pcti', 1.0e-6)  # parallel CTI
        self.full_well = ccd_params.get('full_well', 1e5)  # e-

        amp_bounds = list(self.ccd.values())[0].raw_bounds
        self.scte_matrix = (None if scti == 0
                            else cte_matrix(amp_bounds.xmax, scti))
        self.pcte_matrix = (None if pcti == 0
                            else cte_matrix(amp_bounds.ymax, pcti))

    def apply_cte(self, amp_images):
        """Apply CTI to a list of amp images."""
        for full_segment in amp_images:
            full_arr = full_segment.array
            if self.pcte_matrix is not None:
                for col in range(full_arr.shape[1]):
                    full_arr[:, col] = self.pcte_matrix @ full_arr[:, col]
            if self.scte_matrix is not None:
                for row in range(full_arr.shape[0]):
                    full_arr[row, :] = self.scte_matrix @ full_arr[row, :]
        return amp_images

    def apply_crosstalk(self, amp_arrays):
        """Apply intra-CCD crosstalk to an array of amp data."""
        if self.ccd.xtalk is None:
            return amp_arrays
        output = []
        for amp_index, xtalk_row in enumerate(self.ccd.xtalk):
            output.append(amp_arrays[amp_index] +
                          sum([x*y for x, y in zip(amp_arrays, xtalk_row)]))
        return output

    def build_amp_images(self, rng):
        """Build the amplifier images from the "electron-image".
        The steps are
        * add dark current
        * divide the physical image into amplifier segements
        * apply per-amp gains
        * apply appropriate flips in x- and y-directions to
          get the amp image array in readout order
        * apply intra-CCD crosstalk
        * add prescan and overscan pixels
        * apply charge transfer efficiency effects
        * add bias levels and read noise
        """
        # Bleed trail processing. TODO: Get full_well from the camera.
        self.eimage.array[:] = bleed_eimage(self.eimage.array, full_well=self.full_well)

        # Add dark current.
        dark_time = self.exp_time + self.readout_time
        dark_current = self.dark_current
        poisson = galsim.PoissonDeviate(rng, mean=dark_current*dark_time)
        dc_data = np.zeros(np.prod(self.eimage.array.shape))
        poisson.generate(dc_data)
        self.eimage += dc_data.reshape(self.eimage.array.shape)

        # Partition eimage into amp-level imaging segments, convert to ADUs,
        # and apply the readout flips.
        amp_arrays = []
        for amp in self.ccd.values():
            amp_data = self.eimage[amp.bounds].array/amp.gain
            if amp.raw_flip_x:
                amp_data = amp_data[:, ::-1]
            if amp.raw_flip_y:
                amp_data = amp_data[::-1, :]
            amp_arrays.append(amp_data)

        # Add intra-CCD crosstalk.
        amp_arrays = self.apply_crosstalk(amp_arrays)

        # Construct full segments with prescan and overscan pixels.
        self.amp_images = []
        for amp_data, amp in zip(amp_arrays, self.ccd.values()):
            full_segment = galsim.Image(amp.raw_bounds)
            full_segment[amp.raw_data_bounds].array[:] += amp_data
            self.amp_images.append(full_segment)

        # Apply CTI.
        self.amp_images = self.apply_cte(self.amp_images)

        # Add bias levels and read noise.
        for full_segment in self.amp_images:
            full_segment += self.bias_level
            # Setting gain=0 turns off the addition of Poisson noise,
            # which is already in the e-image, so that only the read
            # noise is added.
            read_noise = galsim.CCDNoise(rng, gain=0,
                                         read_noise=amp.read_noise)
            full_segment.addNoise(read_noise)

    def prepare_hdus(self, rng):
        """
        Create per-amp image HDUs from the eimage and fill the primary
        and image HDU headers.
        """
        # Build per-amp images, adding camera readout features.
        self.build_amp_images(rng)

        # Build HDUs.
        channels = '10 11 12 13 14 15 16 17 07 06 05 04 03 02 01 00'.split()
        x_seg_offset = (1, 2, 3, 4, 5, 6, 7, 8, 8, 7, 6, 5, 4, 3, 2, 1)
        if self.camera_name == 'LsstCamImSim':
            y_seg_offset = (0, 0, 0, 0, 0, 0, 0, 0, 2, 2, 2, 2, 2, 2, 2, 2)
            cd_matrix_sign = -1
        else:
            y_seg_offset = (2, 2, 2, 2, 2, 2, 2, 2, 0, 0, 0, 0, 0, 0, 0, 0)
            cd_matrix_sign = 1
        wcs = self.eimage.wcs
        crpix1, crpix2 = wcs.crpix

        hdus = fits.HDUList(get_primary_hdu(self.eimage, logger=self.logger))
        for amp_num, amp in enumerate(self.amp_images):
            channel = 'C' + channels[amp_num]
            amp_info = self.ccd[channel]
            raw_data_bounds = amp_info.raw_data_bounds
            hdu = fits.CompImageHDU(np.array(amp.array, dtype=np.int32),
                                    compression_type='RICE_1')
            wcs.writeToFitsHeader(hdu.header, self.eimage.bounds)
            hdu.header['EXTNAME'] = 'Segment' + channels[amp_num]
            xsign = -1 if amp_info.raw_flip_x else 1
            ysign = -1 if amp_info.raw_flip_y else 1
            height, width = raw_data_bounds.numpyShape()
            hdu.header['CRPIX1'] = xsign*crpix1 + x_seg_offset[amp_num]*width
            hdu.header['CRPIX2'] = ysign*crpix2 + y_seg_offset[amp_num]*height
            hdu.header['CD1_2'] *= cd_matrix_sign*xsign
            hdu.header['CD2_2'] *= cd_matrix_sign*xsign
            hdu.header['CD1_1'] *= cd_matrix_sign*ysign
            hdu.header['CD2_1'] *= cd_matrix_sign*ysign
            hdu.header['DATASEC'] = section_keyword(raw_data_bounds)
            hdu.header['DETSEC'] = section_keyword(amp_info.bounds,
                                                   flipx=amp_info.raw_flip_x,
                                                   flipy=amp_info.raw_flip_y)
            hdus.append(hdu)
            amp_name = '_'.join((self.det_name, channel))
            self.logger.info("Amp %s has bounds %s.", amp_name,
                             hdu.header['DETSEC'])
        return hdus

    @staticmethod
    def write_raw_file(hdus, file_name):
        """Write the raw data file."""
        hdus[0].header['OUTFILE'] = os.path.basename(file_name)
        hdus.writeto(file_name, overwrite=True)


class CameraReadout(ExtraOutputBuilder):
    """
    This is a GalSim "extra output" builder to write out the amplifier
    file simulating the camera readout of the main "e-image".
    """

    def finalize(self, config, base, main_data, logger):
        """
        This function will use the CcdReadout class to divide the physical
        CCD image into amplifier segments and add readout effects.
        This function will also add header keywords with the amp names
        and pixel geometry, and will package everything up as an
        astropy.io.fits.HDUList.

        Parameters:
           config:     The configuration field for this output object.
           base:       The base configuration dict.
           main_data:  The main file data in case it is needed.
           logger:     If given, a logger object to log progress. [default: None]

        Returns:
           An HDUList of the amplifier images in a CCD.
        """
        logger.warning("Making amplifier images")

        rng = galsim.config.GetRNG(config, base)

        ccd_readout = CcdReadout(main_data[0], logger)

        hdus = ccd_readout.prepare_hdus(rng)
        return hdus

    def writeFile(self, file_name, config, base, logger):
        """Write this output object to a file.

        Parameters:
            file_name:  The file to write to.
            config:     The configuration field for this output object.
            base:       The base configuration dict.
            logger:     If given, a logger object to log progress. [default: None]

        """
        logger.warning("Writing amplifier images to %s", file_name)
        # self.final_data is the output of finalize, which is our list
        # of amp images.
        CcdReadout.write_raw_file(self.final_data, file_name)

RegisterExtraOutput('readout', CameraReadout())
