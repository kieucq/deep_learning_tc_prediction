#!/usr/bin/env python3

"""
This script will extract all patches in a .nc file from NCEP/FNL data
to create binary genesis classification dataset in tfrecords format.
"""
try:
    from .tc_binary_classification_helpers import *
except ImportError:
    from tc_binary_classification_helpers import *

try:
    from .tfrecords_utils import *
except ImportError:
    from tfrecords_utils import *


import argparse
from collections import OrderedDict, namedtuple
from datetime import datetime, timedelta
from multiprocessing import Pool
import numpy as np
import os
import pandas as pd
import tensorflow as tf
from tqdm import tqdm
import xarray as xr


SUBSET = OrderedDict(
    absvprs=(900, 750),
    rhprs=(750,),
    tmpprs=(900, 500),
    hgtprs=(500,),
    vvelprs=(500,),
    ugrdprs=(800, 200),
    vgrdprs=(800, 200),
    tmpsfc=True,
    pressfc=True,
)


def parse_args(args=None):
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '--best-track',
        dest='best_track',
        required=True,
        help='Pattern of best track files.')
    parser.add_argument(
        '--indir',
        dest='indir',
        required=True,
        help='Path to .nc files extracted from The Anh data.')
    parser.add_argument(
        '--leadtime',
        default=0,
        type=int,
        help='Lead time (in hours). Default is 0h.',
    )
    parser.add_argument(
        '--processes',
        default=8,
        type=int,
        help='Number of parallel processes. Default to 8.'
    )
    parser.add_argument(
        '--all-variables',
        dest='all_variables',
        action='store_true',
        help='Whether should we extract all variables or not. Default is False, which means only a subset is extracted.'
    )
    parser.add_argument(
        '--domain-size',
        dest='domain_size',
        default=30,
        type=float,
        help='Size (in degrees) of the extracted domain. Default is 30deg.')
    parser.add_argument(
        '--stride',
        default=5,
        type=float,
        help='Stride (in degrees). Default is 5deg.')
    parser.add_argument(
        '--from-date',
        dest='from_date',
        default='20300101',
        type=lambda d: datetime.strptime(d, '%Y%m%d'),
        help='Extract data from this date onward.')
    parser.add_argument(
        '--till-date',
        dest='till_date',
        default='20350101',
        type=lambda d: datetime.strptime(d, '%Y%m%d'),
        help='Extract data to this date.')
    parser.add_argument(
        '--outfile',
        required=True,
        help='Path to output file.')

    return parser.parse_args(args)


def to_example(value: np.ndarray, pos: np.ndarray, genesis: bool, path: str):
    feature = dict(
        data=numpy_feature(value),
        data_shape=int64_feature(value.shape),
        position=numpy_feature(pos),
        genesis=int64_feature([genesis]),
        filename=bytes_feature(str.encode(path)),
    )
    return tf.train.Example(features=tf.train.Features(feature=feature))


def downscale_ds_to_1deg_resolution(ds: xr.Dataset) -> xr.Dataset:
    latmin, lonmin = tuple(round(ds[dim].values.min()) for dim in ['lat', 'lon'])
    latmax, lonmax = tuple(round(ds[dim].values.max()) for dim in ['lat', 'lon'])
    return ds.interp(
        lat=np.arange(latmin, latmax + 1),
        lon=np.arange(lonmin, lonmax + 1),
        method='linear',
        kwargs={"fill_value": "extrapolate"},)

def fill_missing_values(ds: xr.Dataset) -> xr.Dataset:
    mean_values = ds.mean(dim=['lat', 'lon'], skipna=True)
    return ds.fillna(mean_values)

def has_nan(ds: xr.Dataset, desc):
    for name, values in ds.items():
        isnan = np.isnan(values.values)
        if isnan.ndim == 3:
            isnan = np.any(isnan, axis=(1, 2))
        print(desc, name, isnan)

ProcessArgs = namedtuple('ProcessArgs', ['row', 'domain_size', 'stride', 'all_variables'])
def extract_dataset_samples(args: ProcessArgs) -> list[str]:
    row, domain_size, stride, all_variables = args
    ds = xr.load_dataset(row['Path'], engine='netcdf4')
    # has_nan(ds, 'loading')
    ds = fill_missing_values(ds)
    # has_nan(ds, 'fill missing')
    ds = downscale_ds_to_1deg_resolution(ds)
    # has_nan(ds, 'down scale')
    # raise Error()
    lat, lon = ds['lat'].values, ds['lon'].values
    latmin, latmax = lat.min(), lat.max()
    lonmin, lonmax = lon.min(), lon.max()

    g_lat, g_lon = row['LAT'], row['LON']

    variables_order = list(VARIABLES_ORDER)
    variables_order.remove('capesfc')

    results = []
    for lt in np.arange(latmin, latmax, stride):
        for ln in np.arange(lonmin, lonmax, stride):
            if ((lt + domain_size) > latmax) or ((ln + domain_size) > lonmax):
                continue

            if g_lat is None:
                genesis = False
            else:
                if not isinstance(g_lat, list):
                    g_lat = [g_lat]
                    g_lon = [g_lon]

                genesis = any(
                    (lt < glt < lt + domain_size) and (ln < gln < ln + domain_size)
                    for glt, gln in zip(g_lat, g_lon))

            patch = ds.sel(lat=slice(lt, lt + domain_size), lon=slice(ln, ln + domain_size))
            patch = (extract_subset(patch, SUBSET)
                     if not all_variables
                     else extract_all_variables(patch, variables_order))
            patch_example = to_example(
                patch, np.asarray([lt, ln]), genesis, row['Path'])
            results.append(patch_example.SerializeToString())

    return results


def extract_dataset_samples_parallel(
        genesis_df: pd.DataFrame, outputfile: str, *,
        domain_size: float, stride: float, processes: int, desc: str, all_variables: bool):
    with Pool(processes) as pool:
        tasks = pool.imap_unordered(
            extract_dataset_samples, 
            (ProcessArgs(r, domain_size, stride, all_variables) for _, r in genesis_df.iterrows()))

        with tf.io.TFRecordWriter(outputfile) as writer:
            for results in tqdm(tasks, total=len(genesis_df), desc=desc):
                for r in results:
                    writer.write(r)


def main(args=None):
    args = parse_args(args)
    
    outfile = args.outfile

    files = list_reanalysis_files(args.indir)
    genesis_df, _ = load_best_track_files_theanh(args.best_track)

    # Remove storms that are outside the domain of interest.
    # ds = xr.load_dataset(files['Path'].iloc[0], engine='netcdf4')
    # lat, lon = ds['lat'].values, ds['lon'].values
    # latmin, latmax = lat.min(), lat.max()
    # lonmin, lonmax = lon.min(), lon.max()
    # genesis_df = genesis_df[
    #     (latmin <= genesis_df['LAT']) & (genesis_df['LAT'] <= latmax)
    #     & (lonmin <= genesis_df['LON']) & (genesis_df['LON'] <= lonmax)]

    # Combine best track with data that we have.
    # In this step, all negative samples
    # (observations without TC genesis) are removed.
    files['OriginalDate'] = files['Date'].copy()
    files['Date'] = files['Date'].apply(
        lambda date: date + timedelta(hours=args.leadtime))
    genesis_df = files.merge(genesis_df, how='inner', on='Date')
    genesis_df = genesis_df.groupby('Path').agg({
        'OriginalDate': 'first',
        'LAT': lambda x: x.iloc[0] if len(x) == 1 else list(x),
        'LON': lambda x: x.iloc[0] if len(x) == 1 else list(x),
        # 'BASIN': lambda x: x.iloc[0] if len(x) == 1 else list(x), 
        'SID': lambda x: x.iloc[0] if len(x) == 1 else list(x), 
        'Date': lambda x: x.iloc[0] if len(x) == 1 else list(x), 
    })
    genesis_df['Path'] = genesis_df.index

    dates = genesis_df['OriginalDate']
    genesis_df = genesis_df[(dates >= args.from_date) & (dates < args.till_date)]

    # Create output directories.
    outdir = os.path.dirname(outfile)
    os.makedirs(outdir, exist_ok=True)

    # Extract datasets.
    assert not os.path.isfile(outfile), f'Output file: {outfile=} exists!'
    extract_dataset_samples_parallel(
        genesis_df,
        outfile,
        domain_size=args.domain_size, stride=args.stride,
        processes=args.processes,
        desc=f'Extracting from {args.from_date} to {args.till_date}',
        all_variables=args.all_variables)


if __name__ == '__main__':
    main()
