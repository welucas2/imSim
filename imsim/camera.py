import os
import pickle
import copy
import numpy as np
import galsim
from galsim.config import RegisterInputType, InputLoader
import lsst.utils


__all__ = ['get_camera', 'Camera']


def get_gs_bounds(bbox):
    """
    Return a galsim.BoundsI object created from an lsst.afw.Box2I object.
    """
    return galsim.BoundsI(xmin=bbox.getMinX() + 1, xmax=bbox.getMaxX() + 1,
                          ymin=bbox.getMinY() + 1, ymax=bbox.getMaxY() + 1)


class Amp:
    """
    Class to contain the pixel geometry and electronic readout properties
    of the amplifier segments in the Rubin Camera CCDs.
    """
    def __init__(self):
        self.bounds = None
        self.raw_flip_x = None
        self.raw_flip_y = None
        self.gain = None
        self.raw_bounds = None
        self.raw_data_bounds = None
        self.read_noise = None
        self.bias_level = None
        self.lsst_amp = None

    def update(self, other):
        """
        Method to copy the properties of another Amp object.
        """
        self.__dict__.update(other.__dict__)

    @staticmethod
    def make_amp_from_lsst(lsst_amp, bias_level=1000.):
        """
        Static function to create an Amp object, extracting its properties
        from an lsst.afw.cameraGeom.Amplifier object.

        Parameters
        ----------
        lsst_amp : lsst.afw.cameraGeom.Amplifier
           The LSST Science Pipelines class representing an amplifier
           segment in a CCD.
        bias_level : float [1000.]
           The bias level (ADU) to use since the camerGeom.Amplifier
           object doesn't have a this value encapsulated.

        Returns
        -------
        Amp object
        """
        my_amp = Amp()
        my_amp.lsst_amp = lsst_amp
        my_amp.bounds = get_gs_bounds(lsst_amp.getBBox())
        my_amp.raw_flip_x = lsst_amp.getRawFlipX()
        my_amp.raw_flip_y = lsst_amp.getRawFlipY()
        my_amp.gain = lsst_amp.getGain()
        my_amp.raw_bounds = get_gs_bounds(lsst_amp.getRawBBox())
        my_amp.raw_data_bounds = get_gs_bounds(lsst_amp.getRawDataBBox())
        my_amp.read_noise = lsst_amp.getReadNoise()
        my_amp.bias_level = bias_level
        return my_amp

    def __getattr__(self, attr):
        """Provide access to the attributes of the underlying lsst_amp."""
        return getattr(self.lsst_amp, attr)

class CCD(dict):
    """
    A dict subclass to contain the Amp representations of a CCD's
    amplifier segments along with the pixel bounds of the CCD in focal
    plane coordinates, as well as other CCD-level information such as
    the crosstalk between amps.  Amp objects are keyed by LSST amplifier
    name, e.g., 'C10'.

    """
    def __init__(self):
        super().__init__()
        self.bounds = None
        self.xtalk = None
        self.lsst_detector = None

    def update(self, other):
        """
        Method to copy the properties of another CCD object.
        """
        self.__dict__.update(other.__dict__)
        for key, value in other.items():
            if not key in self:
                self[key] = Amp()
            self[key].update(value)

    @staticmethod
    def make_ccd_from_lsst(lsst_ccd):
        """
        Static function to create a CCD object, extracting its properties
        from an lsst.afw.cameraGeom.Detector object, including CCD and
        amp-level bounding boxes, and intra-CCD crosstalk, if it's
        available.

        Parameters
        ----------
        lsst_ccd : lsst.afw.cameraGeom.Detector
           The LSST Science Pipelines class representing a CCD.

        Returns
        -------
        CCD object

        """
        my_ccd = CCD()
        my_ccd.bounds = get_gs_bounds(lsst_ccd.getBBox())
        my_ccd.lsst_ccd = lsst_ccd
        for lsst_amp in lsst_ccd:
            my_ccd[lsst_amp.getName()] = Amp.make_amp_from_lsst(lsst_amp)
        if lsst_ccd.hasCrosstalk():
            my_ccd.xtalk = lsst_ccd.getCrosstalk()
        return my_ccd

    def __getattr__(self, attr):
        """Provide access to the attributes of the underlying lsst_ccd."""
        return getattr(self.lsst_ccd, attr)


_camera_cache = {}
def get_camera(camera='LsstCam'):
    """
    Return an lsst camera object.

    Parameters
    ----------
    camera : str
       The class name of the LSST camera object. Valid names
       are 'LsstCam', 'LsstComCam', 'LsstCamImSim'. [default: 'LsstCam']

    Returns
    -------
    lsst.afw.cameraGeom.Camera
    """
    valid_cameras = ('LsstCam', 'LsstComCam', 'LsstCamImSim')
    if camera not in valid_cameras:
        raise ValueError('Invalid camera: %s', camera)
    if camera not in _camera_cache:
        _camera_cache[camera] = lsst.utils.doImport('lsst.obs.lsst.' + camera)().getCamera()
    return _camera_cache[camera]


class Camera(dict):
    """
    Class to represent the LSST Camera as a dictionary of CCD objects,
    keyed by the CCD name in the focal plane, e.g., 'R01_S00'.
    """
    def __init__(self, camera_class='LsstCam'):
        """
        Initialize a Camera object from the lsst instrument class.
        """
        super().__init__()
        self.lsst_camera = get_camera(camera_class)
        for lsst_ccd in self.lsst_camera:
            self[lsst_ccd.getName()] = CCD.make_ccd_from_lsst(lsst_ccd)

    def update(self, other):
        """
        Method to copy the properties of the CCDs in this object from
        another Camera object.
        """
        self.__dict__.update(other.__dict__)
        for key, value in other.items():
            if not key in self:
                self[key] = CCD()
            self[key].update(value)

    def __getattr__(self, attr):
        """Provide access to the attributes of the underlying lsst_camera."""
        return getattr(self.lsst_camera, attr)
