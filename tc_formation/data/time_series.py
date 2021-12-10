import abc
from datetime import datetime, timedelta
from functools import partial
import tc_formation.data.label as label
import tc_formation.data.utils as tcd_utils
import numpy as np
import os
import pandas as pd
import tensorflow as tf
from typing import List, Tuple
import xarray as xr


class TimeSeriesTropicalCycloneDataLoader:
    def __init__(self, data_shape, previous_hours:List[int] = [6, 12, 18], subset=None):
        self._data_shape = data_shape
        self._previous_hours = previous_hours
        self._subset = subset

    def _load_tc_csv(self, data_path, leadtimes: List[int] = None) -> pd.DataFrame:
        return label.load_label(
                data_path,
                group_observation_by_date=True,
                leadtime=leadtimes)

    @classmethod
    def _add_previous_observation_data_paths(cls, path: str, previous_times: List[int]) -> List[str]:
        dirpath = os.path.dirname(path)
        name = os.path.basename(path)
        name, _ = os.path.splitext(name)
        # The date of the observation is embedded in the filename: `fnl_%Y%m%d_%H_%M.nc`
        date_part = ''.join(list(name)[4:])

        date = datetime.strptime(date_part, '%Y%m%d_%H_%M')

        previous_times = previous_times.copy()
        previous_times.sort()
        dates = [date - timedelta(hours=time) for time in previous_times]
        dates += [date]
        return [os.path.join(dirpath, f"fnl_{d.strftime('%Y%m%d_%H_%M')}.nc") for d in dates]

    @classmethod
    def _are_valid_paths(cls, paths: List[str]) -> bool:
        return all([os.path.isfile(p) for p in paths])

    @abc.abstractmethod
    def _process_to_dataset(self, tc_df: pd.DataFrame) -> tf.data.Dataset:
        pass

    def load_dataset(self, data_path, shuffle=False, batch_size=64, leadtimes: List[int]=None):
        cls = TimeSeriesTropicalCycloneDataLoader

        # Load TC dataframe.
        tc_df = self._load_tc_csv(data_path, leadtimes)
        tc_df['Path'] = tc_df['Path'].apply(
                partial(cls._add_previous_observation_data_paths, previous_times=self._previous_hours))
        tc_df = tc_df[tc_df['Path'].apply(cls._are_valid_paths)]

        # Convert to tf dataset.
        dataset = self._process_to_dataset(tc_df)

        if shuffle:
            dataset = dataset.shuffle(batch_size * 3)

        dataset = dataset.cache()
        dataset = dataset.batch(batch_size)
        return dataset.prefetch(1)

class TimeSeriesTropicalCycloneWithGridProbabilityDataLoader(TimeSeriesTropicalCycloneDataLoader):
    def __init__(self, tc_avg_radius_lat_deg=3, softmax_output=True, clip_threshold=0.1, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._softmax_output = softmax_output
        self._tc_avg_radius_lat_deg = tc_avg_radius_lat_deg
        self._clip_threshold = clip_threshold

    def _process_to_dataset(self, tc_df: pd.DataFrame) -> tf.data.Dataset:
        cls = TimeSeriesTropicalCycloneWithGridProbabilityDataLoader

        dataset = tf.data.Dataset.from_tensor_slices({
            'Path': np.asarray(tc_df['Path'].sum()).reshape((-1, len(self._previous_hours) + 1)),
            'TC': tc_df['TC'],
            'Latitude': tc_df['Latitude'],
            'Longitude': tc_df['Longitude'],
        })
        
        dataset = dataset.map(
            lambda row: tcd_utils.new_py_function(
                    lambda row: cls._load_reanalysis_and_gt(
                            [path.decode('utf-8') for path in row['Path'].numpy()],
                            self._subset,
                            row['TC'].numpy(),
                            self._data_shape,
                            row['Latitude'].numpy(),
                            row['Longitude'].numpy(),
                            self._tc_avg_radius_lat_deg,
                            self._clip_threshold,
                            self._softmax_output,
                        ),
                    inp=[row],
                    Tout=[tf.float32, tf.float32],
                    name='load_observation_and_gt',
                ),
            num_parallel_calls=tf.data.AUTOTUNE,
            deterministic=False,
        )

        # Tensorflow should figure out the shape of the output of previous map,
        # but it doesn't, so we have to do it ourself.
        # https://github.com/tensorflow/tensorflow/issues/31373#issuecomment-524666365
        dataset = dataset.map(partial(
            cls._set_dataset_shape,
            shape=(len(self._previous_hours) + 1,) + self._data_shape,
            softmax_output=self._softmax_output))
        
        return dataset

    @classmethod
    def _set_dataset_shape(
            cls,
            data: tf.Tensor,
            gt: tf.Tensor,
            shape: tuple,
            softmax_output: bool):
        data.set_shape(shape)
        gt.set_shape(shape[1:3] + ((2,) if softmax_output else (1,)))
        return data, gt

    @classmethod
    def _load_reanalysis_and_gt(
            cls,
            paths: List[str],
            subset: dict,
            has_tc: bool,
            data_shape: tuple,
            tc_latitudes: float,
            tc_longitudes: float,
            tc_avg_radius_lat_deg: int,
            clip_threshold: float,
            softmax_output: bool,
        ) -> np.ndarray:

        datasets = []
        for path in paths:
            dataset = xr.open_dataset(path, engine='netcdf4')
            latitudes = dataset['lat']
            longitudes = dataset['lon']
            dataset = extract_variables_from_dataset(dataset, subset)
            datasets.append(np.expand_dims(dataset, axis=0))
        datasets = np.concatenate(datasets, axis=0)

        gt = cls._create_probability_grid_gt(
                has_tc,
                data_shape,
                latitudes,
                longitudes,
                tc_latitudes,
                tc_longitudes,
                softmax_output,
                tc_avg_radius_lat_deg,
                clip_threshold
            )

        return datasets, gt

    @classmethod
    def _create_probability_grid_gt(
            cls,
            has_tc: bool,
            data_shape: Tuple[int, int, int],
            latitudes: list,
            longitudes: list,
            tc_latitudes: float,
            tc_longitudes: float,
            softmax_output: bool,
            tc_avg_radius_lat_deg: int,
            clip_threshold: float):
        groundtruth = np.zeros(data_shape[:-1])
        x, y = np.meshgrid(longitudes, latitudes)

        if has_tc:
            lats = tc_latitudes
            lons = tc_longitudes
            lats = lats if isinstance(lats, list) else [lats]
            lons = lons if isinstance(lons, list) else [lons]
            for lat, lon in zip(lats, lons):
                x_diff = x - lon
                y_diff = y - lat

                # RBF kernel.
                prob = np.exp(-(x_diff * x_diff + y_diff * y_diff)/(2 * tc_avg_radius_lat_deg ** 2))
                prob[prob < clip_threshold] = 0
                groundtruth += prob

        if not softmax_output:
            new_groundtruth = np.zeros(np.shape(groundtruth) + (1,))
            new_groundtruth[:, :] = np.where(groundtruth > 0, 1, 0)
        else:
            new_groundtruth = np.zeros(np.shape(groundtruth) + (2,))
            new_groundtruth[:, :, 0] = np.where(groundtruth > 0, 0, 1)
            new_groundtruth[:, :, 1] = np.where(groundtruth > 0, 1, 0)

        return new_groundtruth

def extract_variables_from_dataset(dataset: xr.Dataset, subset: dict = None):
    data = []
    for var in dataset.data_vars:
        if subset is not None and var in subset:
            values = dataset[var].sel(lev=subset[var]).values
        else:
            values = dataset[var].values

        # For 2D dataarray, make it 3D.
        if len(np.shape(values)) != 3:
            values = np.expand_dims(values, 0)

        data.append(values)

    # Reshape data so that it have channel_last format.
    data = np.concatenate(data, axis=0)
    data = np.moveaxis(data, 0, -1)

    return data
