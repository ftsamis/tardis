#Utility functions for the IO part of TARDIS

import os
import re
import pandas as pd
import numpy as np
import collections
from collections import OrderedDict
import yaml
from astropy import constants, units as u
from tardis.util import element_symbol2atomic_number

import logging
logger = logging.getLogger(__name__)

float_pattern = r"""    # A numeric string consists of:
    (?P<sign>[-+])?              # an optional sign, followed by either...

    (

        (?=\d|\.\d)              # ...a number (with at least one digit)

        (?P<int>\d*)             # having a (possibly empty) integer part

        (\.(?P<frac>\d*))?       # followed by an optional fractional part

        (E(?P<exp>[-+]?\d+))?    # followed by an optional exponent, or...

    |

        Inf(inity)?              # ...an infinity, or...

    |
        NaN                      # NaN
    )
"""

unit_pattern = r"[a-zA-Z1(][\w\s/^*()+-]*"

float_regex = re.compile("^" + float_pattern + "$", re.VERBOSE | re.IGNORECASE)
quantity_regex = re.compile("^" + float_pattern + "\s+" + unit_pattern + "$",
                            re.VERBOSE | re.IGNORECASE)


def quantity_from_str(text):
    """
    Convert a string to `astropy.units.Quantity`
    Parameters
    ----------
    text:
        The string to convert to `astropy.units.Quantity`
    Returns
    -------
    `astropy.units.Quantity`
    """
    value_str, unit = text.split(None, 1)
    value = float(value_str)
    if unit.strip() == 'log_lsun':
        value = 10 ** (value + np.log10(constants.L_sun.cgs.value))
        unit = 'erg/s'
    return u.Quantity(value, unit)


class YAMLLoader(yaml.Loader):
    """
    A custom YAML loader containing all the constructors required
    to properly parse the tardis configuration.
    """

    def construct_quantity(self, node):
        """
        A constructor for converting quantity-like YAML nodes to
        `astropy.units.Quantity` objects.

        Parameters
        ----------

        node:
            The YAML node to be constructed

        Returns
        -------

        `astropy.units.Quantity`

        """
        data = self.construct_scalar(node)
        return quantity_from_str(data)

    def mapping_constructor(self, node):
        return OrderedDict(self.construct_pairs(node))

YAMLLoader.add_constructor(u'!quantity', YAMLLoader.construct_quantity)
YAMLLoader.add_implicit_resolver(u'!quantity', quantity_regex,
                                 list('-+0123456789.nNiI'))
YAMLLoader.add_implicit_resolver(u'tag:yaml.org,2002:float', float_regex,
                                 list('-+0123456789.nNiI'))
YAMLLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                           YAMLLoader.mapping_constructor)

def yaml_load_file(filename, loader=yaml.Loader):
    with open(filename) as stream:
        return yaml.load(stream, loader)

def yaml_load_config_file(filename):
    return yaml_load_file(filename, YAMLLoader)

def parse_abundance_dict_to_dataframe(abundance_dict):
    atomic_number_dict = dict([(element_symbol2atomic_number(symbol), abundance_dict[symbol])
                                   for symbol in abundance_dict])
    atomic_numbers = sorted(atomic_number_dict.keys())

    abundances = pd.Series([atomic_number_dict[z] for z in atomic_numbers], index=atomic_numbers)

    abundance_norm = abundances.sum()
    if abs(abundance_norm - 1) > 1e-12:
        logger.warn('Given abundances don\'t add up to 1 (value = %g) - normalizing', abundance_norm)
        abundances /= abundance_norm

    return abundances


def traverse_configs(base, other, func, *args):
    """
    Recursively traverse a base dict or list along with another one
    calling `func` for leafs of both objects.

    Parameters
    ----------
    base:
        The object on which the traversing is done
    other:
        The object which is traversed along with `base`
    func:
        A function called for each leaf of `base` and the correspnding leaf of `other`
        Signature: `func(item1, item2, *args)`
    args:
        Arguments passed into `func`

    """
    if isinstance(base, collections.Mapping):
        for k in base:
            traverse_configs(base[k], other[k], func, *args)
    elif isinstance(base, collections.Iterable) and not isinstance(base, basestring) and not hasattr(base, 'shape'):
        for val1, val2 in zip(base, other):
            traverse_configs(val1, val2, func, *args)
    else:
        func(base, other, *args)


def assert_equality(item1, item2):
    assert type(item1) is type(item2)
    try:
        if hasattr(item1, 'unit'):
            assert item1.unit == item2.unit
        assert np.allclose(item1, item2, atol=0.0)
    except (ValueError, TypeError):
        assert item1 == item2


def check_equality(item1, item2):
    try:
        traverse_configs(item1, item2, assert_equality)
    except AssertionError:
        return False
    else:
        return True


def to_hdf(path_or_buf, path, elements, complevel=9, complib='blosc'):
    """
    A function to uniformly store TARDIS data
    to an HDF file.

    Scalars will be stored in a Series under path/scalars
    1D arrays will be stored under path/property_name as distinct Series
    2D arrays will be stored under path/property_name as distinct DataFrames

    Units will be stored as their CGS value

    Parameters
    ----------
    path_or_buf:
        Path or buffer to the HDF store
    path: str
        Path inside the HDF store to store the `elements`
    elements: dict
        A dict of property names and their values to be
        stored.

    Returns
    -------

    """
    scalars = {}
    for key, value in elements.iteritems():
        if hasattr(value, 'cgs'):
            value = value.cgs.value
        if np.isscalar(value):
            scalars[key] = value
        elif hasattr(value, 'shape'):
            if value.ndim == 1:
                # This try,except block is only for model.plasma.levels
                try:
                    pd.Series(value).to_hdf(path_or_buf,
                                            os.path.join(path, key))
                except NotImplementedError:
                    pd.DataFrame(value).to_hdf(path_or_buf,
                                               os.path.join(path, key))
            else:
                pd.DataFrame(value).to_hdf(path_or_buf, os.path.join(path, key))
        else:
            data = pd.DataFrame([value])
            data.to_hdf(path_or_buf, os.path.join(path, key))

    if scalars:
        scalars_series = pd.Series(scalars)

        # Unfortunately, with to_hdf we cannot append, so merge beforehand
        scalars_path = os.path.join(path, 'scalars')
        with pd.HDFStore(path_or_buf, complevel=complevel, complib=complib) as store:
            if scalars_path in store:
                scalars_series = store[scalars_path].append(scalars_series)
        scalars_series.to_hdf(path_or_buf, os.path.join(path, 'scalars'))
