# This file is part of opentaps Smart Energy Applications Suite (SEAS).

# opentaps Smart Energy Applications Suite (SEAS) is free software:
# you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# opentaps Smart Energy Applications Suite (SEAS) is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public License
# along with opentaps Smart Energy Applications Suite (SEAS).
# If not, see <https://www.gnu.org/licenses/>.

import logging
import eemeter
import pytz
from math import isnan
from datetime import date
from datetime import timedelta
from datetime import datetime
from io import StringIO
from eemeter import io as eeio
from ..core.models import MeterProduction
from ..core.models import SiteView
from ..core.models import SiteWeatherStations
from ..core.models import Meter
from ..core.models import MeterHistory
from ..core.models import MeterFinancialValue
from ..core.models import FinancialTransaction
from ..core.models import WeatherStation
from ..core.models import WeatherHistory
from .models import BaselineModel
from django.utils.timezone import now

logger = logging.getLogger(__name__)


def setup_demo_sample_models(site_id, meter_id=None, description=None, calc_savings=False):
    # this create a test sample meter with both hourly and daily model
    # using sample meter and temperature data for the given site id

    # use the hourly sample data
    meter_data, temperature_data, sample_metadata = (
        eemeter.load_sample("il-electricity-cdd-hdd-hourly")
    )

    source = 'eemeter_sample'
    # throw an exception is Site is not found
    try:
        site = SiteView.objects.get(object_id=site_id)
    except SiteView.DoesNotExist:
        site = SiteView.objects.get(entity_id=site_id)

    min_datetime = None
    max_datetime = None
    yesterday = date.today() - timedelta(days=1)
    # create a dummy weather station for the sample data if it did not alredy exist
    try:
        ws = WeatherStation.objects.get(weather_station_id='eemeter_ws')
    except WeatherStation.DoesNotExist:
        logger.info('setup_demo_sample_models: creating Sample WeatherStation eemeter_ws ...')
        ws = WeatherStation.objects.create(
            weather_station_id='eemeter_ws',
            station_name='Sample Station',
            source=source,
            elevation_uom_id='length_m')
        # associate it to the site
        SiteWeatherStations.objects.create(
            weather_station=ws,
            site_id=site.entity_id,
            source=source)

    WeatherHistory.objects.filter(weather_station=ws).delete()
    logger.info('setup_demo_sample_models: adding Sample WeatherStation data ...')
    temp_items = []
    for d, t in temperature_data.iteritems():
        item = {"d": d, "t": t}
        temp_items.append(item)

    ts = datetime(year=yesterday.year, month=yesterday.month, day=yesterday.day, hour=23,
                  minute=0, second=0, microsecond=0, tzinfo=pytz.UTC)
    for item in reversed(temp_items):
        item["d"] = ts
        ts = ts - timedelta(minutes=60)

    # load the temperature data, this is given in F
    for item in temp_items:
        tc = (item["t"] - 32.0) * 5 / 9
        WeatherHistory.objects.create(weather_station=ws, as_of_datetime=item["d"],
                                      temp_f=item["t"], temp_c=tc, source=source)
        if not min_datetime or item["d"] < min_datetime:
            min_datetime = item["d"]
        if not max_datetime or item["d"] > max_datetime:
            max_datetime = item["d"]

    if not meter_id:
        meter_id = '{}-sample_meter'.format(site_id)
    if not description:
        description = 'Sample Meter'

    # cleanup previous entries as needed
    MeterHistory.objects.filter(meter_id=meter_id).delete()
    MeterFinancialValue.objects.filter(meter_id=meter_id).delete()
    FinancialTransaction.objects.filter(meter_id=meter_id).delete()
    Meter.objects.filter(meter_id=meter_id).delete()
    # create the meter
    logger.info('setup_demo_sample_models: creating Sample Meter %s ...', meter_id)
    meter = Meter.objects.create(
        meter_id=meter_id,
        site_id=site.entity_id,
        description=description,
        account_number='123456789',
        utility_id='USA_EIA_11208',
        weather_station_id='eemeter_ws')
    # setup the meter data (note this is slightly different format than temps)
    logger.info('setup_demo_sample_models: adding Sample Meter %s data ...', meter_id)
    meter_items = []
    for d, v in meter_data.iterrows():
        item = {"d": d, "v": v}
        meter_items.append(item)

    ts = datetime(year=yesterday.year, month=yesterday.month, day=yesterday.day, hour=23,
                  minute=0, second=0, microsecond=0, tzinfo=pytz.UTC)
    for item in reversed(meter_items):
        item["d"] = ts
        ts = ts - timedelta(minutes=60)

    for item in meter_items:
        MeterHistory.objects.create(meter=meter, as_of_datetime=item["d"], value=item["v"].value,
                                    uom_id='energy_kWh', source=source)
        if not min_datetime or item["d"] < min_datetime:
            min_datetime = item["d"]
        if not max_datetime or item["d"] > max_datetime:
            max_datetime = item["d"]

    baseline_end_date = min_datetime + timedelta(days=365)
    if baseline_end_date > max_datetime:
        baseline_end_date = max_datetime
    # create both models
    frequency = 'hourly'
    data = read_meter_data(meter, freq=frequency, end=baseline_end_date)
    model = get_model_for_freq(data, frequency)
    baseline_model = save_model(model,
                                meter_id=meter.meter_id,
                                description='CalTrack Hourly Model',
                                data=data,
                                frequency=frequency,
                                from_datetime=data['start'],
                                thru_datetime=data['end'])

    if calc_savings:
        calc_meter_savings(meter_id, baseline_model.id, baseline_end_date, max_datetime)
    frequency = 'daily'
    data = read_meter_data(meter, freq=frequency, end=baseline_end_date)
    model = get_model_for_freq(data, frequency)

    baseline_model = save_model(model,
                                meter_id=meter.meter_id,
                                description='CalTrack Daily Model',
                                data=data,
                                frequency=frequency,
                                from_datetime=data['start'],
                                thru_datetime=data['end'])

    if calc_savings:
        calc_meter_savings(meter_id, baseline_model.id, baseline_end_date, max_datetime)

    return site, meter, baseline_model


def get_daily_sample_data():
    meter_data, temperature_data, sample_metadata = (
        eemeter.load_sample("il-electricity-cdd-hdd-daily")
    )
    return read_sample_data(meter_data, temperature_data, sample_metadata)


def get_hourly_sample_data():
    meter_data, temperature_data, sample_metadata = (
        eemeter.load_sample("il-electricity-cdd-hdd-hourly")
    )
    return read_sample_data(meter_data, temperature_data, sample_metadata)


def read_sample_data(meter_data, temperature_data, sample_metadata):
    # the dates if an analysis "blackout" period during which a project was performed.
    blackout_start_date = sample_metadata["blackout_start_date"]
    blackout_end_date = sample_metadata["blackout_end_date"]

    # get meter data suitable for fitting a baseline model
    baseline_meter_data, warnings = eemeter.get_baseline_data(
        meter_data, end=blackout_start_date, max_days=365
    )

    # force alignment of weather data to the read meter data
    start = meter_data.iloc[0].name.to_pydatetime()
    end = meter_data.iloc[-1].name.to_pydatetime()

    return {
        'meter_uom_id': 'energy_kWh',
        'meter_data': meter_data,
        'temperature_data': temperature_data,
        'sample_metadata': sample_metadata,
        'blackout_start_date': blackout_start_date,
        'blackout_end_date': blackout_end_date,
        'baseline_meter_data': baseline_meter_data,
        'start': start,
        'end': end
    }


def read_meter_data(meter, blackout_start_date=None, blackout_end_date=None, freq=None, start=None, end=None, uom=None):
    # get the meter data from the meter history
    logger.info('read_meter_data: freq %s', freq)
    out = StringIO()
    # read the meter data, returns the unit of the meter data
    m_uom = meter.write_meter_data_csv(
            out,
            columns=[{'as_of_datetime': 'start'}, {'value': 'value'}],
            start=start, end=end)
    out.seek(0)
    if freq not in ("hourly", "daily"):
        freq = None
    meter_data = eeio.meter_data_from_csv(out, freq=freq)
    logger.info('read_meter_data: meter_data %s', meter_data)

    # force alignment of weather data to the read meter data
    start = meter_data.iloc[0].name.to_pydatetime()
    end = meter_data.iloc[-1].name.to_pydatetime()
    logger.info('read_meter_data: meter_data from %s to %s', start, end)

    # get the temperature data from the meter linked weather stations
    out = StringIO()
    meter.write_weather_data_csv(out, columns=[{'as_of_datetime': 'dt'}, {'temp_f': 'tempF'}], start=start, end=end)
    out.seek(0)
    temperature_data = eeio.temperature_data_from_csv(out, freq="hourly")
    logger.info('read_meter_data: temperature_data %s', temperature_data)

    # we end the model on the given blackout_start_date else end it on the last data
    blm_end = blackout_start_date or end
    logger.info('read_meter_data: getting baseline_meter_data ending %s', blm_end)
    # get meter data suitable for fitting a baseline model
    baseline_meter_data, warnings = eemeter.get_baseline_data(
        meter_data, end=blm_end, max_days=365
    )

    logger.info('read_meter_data: baseline_meter_data %s', baseline_meter_data)

    start = baseline_meter_data.iloc[0].name.to_pydatetime()
    end = baseline_meter_data.iloc[-1].name.to_pydatetime()
    logger.info('read_meter_data: baseline_meter_data from %s to %s', start, end)

    logger.info('read_meter_data: DONE')

    return {
        'meter_uom_id': m_uom.uom_id if m_uom else None,
        'meter_data': meter_data,
        'temperature_data': temperature_data,
        'blackout_start_date': blackout_start_date,
        'blackout_end_date': blackout_end_date,
        'baseline_meter_data': baseline_meter_data,
        'start': start,
        'end': end
    }


def get_model_for_freq(data, freq, **kwargs):
    if freq == 'hourly':
        return get_hourly_model(data)
    elif freq == 'daily':
        return get_daily_model(data, **kwargs)
    else:
        raise Exception("Model frequency must be hourly or daily")


def get_daily_model(data, **kwargs):

    logger.info('get_daily_model: ...')
    # create a design matrix (the input to the model fitting step)
    logger.info('get_daily_model: creating baseline_design_matrix ...')
    baseline_design_matrix = eemeter.create_caltrack_daily_design_matrix(
        data['baseline_meter_data'], data['temperature_data'],
    )

    # build a CalTRACK model
    logger.info('get_daily_model: building CalTRACK model ...')
    baseline_model = eemeter.fit_caltrack_usage_per_day_model(
        baseline_design_matrix,
        **kwargs
    )

    logger.info('get_daily_model: DONE')
    return baseline_model


def get_hourly_model(data):
    logger.info('get_hourly_model: ...')
    # create a design matrix for occupancy and segmentation
    logger.info('get_hourly_model: creating baseline_design_matrix ...')
    preliminary_design_matrix = (
        eemeter.create_caltrack_hourly_preliminary_design_matrix(
            data['baseline_meter_data'], data['temperature_data'],
        )
    )

    # build 12 monthly models - each step from now on operates on each segment
    logger.info('get_hourly_model: creating segment_time_series ...')
    segmentation = eemeter.segment_time_series(
        preliminary_design_matrix.index,
        'three_month_weighted'
    )

    # assign an occupancy status to each hour of the week (0-167)
    logger.info('get_hourly_model: creating occupancy_lookup ...')
    occupancy_lookup = eemeter.estimate_hour_of_week_occupancy(
        preliminary_design_matrix,
        segmentation=segmentation,
    )

    # assign temperatures to bins
    logger.info('get_hourly_model: creating temperature_bins ...')
    occupied_temperature_bins, unoccupied_temperature_bins = eemeter.fit_temperature_bins(
        preliminary_design_matrix,
        segmentation=segmentation,
        occupancy_lookup=occupancy_lookup,
    )

    # build a design matrix for each monthly segment
    logger.info('get_hourly_model: creating segmented_design_matrices ...')
    segmented_design_matrices = (
        eemeter.create_caltrack_hourly_segmented_design_matrices(
            preliminary_design_matrix,
            segmentation,
            occupancy_lookup,
            occupied_temperature_bins,
            unoccupied_temperature_bins,
        )
    )

    # build a CalTRACK hourly model
    logger.info('get_hourly_model: building CalTRACK model ...')
    baseline_model = eemeter.fit_caltrack_hourly_model(
        segmented_design_matrices,
        occupancy_lookup,
        occupied_temperature_bins,
        unoccupied_temperature_bins,
    )

    logger.info('get_hourly_model: DONE')
    return baseline_model


def save_model(model, meter_id=None, frequency=None, description=None, from_datetime=None,
               thru_datetime=None, data=None, progress_observer=None, model_params={}):
    plot_data = None
    uom_id = 'energy_kWh'
    if data and data['meter_uom_id']:
        uom_id = data['meter_uom_id']
    if data and hasattr(model, 'plot'):
        if progress_observer:
            progress_observer.add_progress(description='Plotting model energy signature ...')
        logger.info('save_model: plotting model ...')
        try:
            from matplotlib.figure import Figure
            from io import BytesIO
            import base64

            fig = Figure(figsize=(10, 4))
            ax = eemeter.plot_energy_signature(data['meter_data'], data['temperature_data'], figure=fig)
            model.plot(
                ax=ax, figure=fig, candidate_alpha=0.02, with_candidates=True, temp_range=(-5, 88)
            )
            buf = BytesIO()
            fig.savefig(buf, format="png")
            # Embed the result in the html output.
            plot_data = base64.b64encode(buf.getbuffer()).decode("ascii")
            logger.info('save_model: plotting model DONE')
        except Exception as e:
            logger.exception(e)
            # could be an error during the model generation ..
            logger.error('Check model warnings: %s', model.warnings)
            if model.warnings:
                for w in model.warnings:
                    if w.qualified_name == 'eemeter.caltrack_daily.select_best_candidate.no_candidates':
                        raise Exception(w.description)

    # persist the given model in the DB
    if progress_observer:
        progress_observer.add_progress(description='Saving model ...')
    return BaselineModel.objects.create(
        data=model.json(),
        model_class=model.__class__.__name__,
        meter_id=meter_id,
        frequency=frequency,
        from_datetime=from_datetime,
        thru_datetime=thru_datetime or now(),
        description=description,
        model_params=model_params,
        plot_data=plot_data,
        uom_id=uom_id)


def load_model(model):
    # load a model from a persisted instance
    # check that the model class exists and supports from_json
    # this throws an exeption if the class does not exist
    clazz = getattr(eemeter, model.model_class)
    # this throws an exeption if the class does not have a from_json method
    m = clazz.from_json(model.data)
    return m


def get_savings(data, baseline_model):
    # get a year of reporting period data
    reporting_meter_data, warnings = eemeter.get_reporting_data(
        data['meter_data'], start=data['blackout_end_date'], max_days=365
    )

    # compute metered savings for the year of the reporting period we've selected
    metered_savings_dataframe, error_bands = eemeter.metered_savings(
        baseline_model, reporting_meter_data,
        data['temperature_data'], with_disaggregated=True
    )

    # total metered savings
    total_metered_savings = metered_savings_dataframe.metered_savings.sum()

    return {
        'reporting_meter_data': reporting_meter_data,
        'total_savings': total_metered_savings,
        'metered_savings': metered_savings_dataframe,
        'error_bands': error_bands
    }


def calc_meter_savings(meter_id, model_id, start, end, progress_observer=None):
    logger.info('calc_meter_savings: for Meter %s, from %s to %s, model id %s', meter_id, start, end, model_id)

    meter = Meter.objects.get(meter_id=meter_id)
    model = BaselineModel.objects.get(id=model_id)

    if progress_observer:
        progress_observer.set_progress(1, 4, description='Load model ...')

    m = load_model(model)
    data = read_meter_data(meter, freq=model.frequency, start=start, end=end, uom=model.uom)

    savings = get_savings(data, m)
    logger.info('calc_meter_savings: got saving = {}'.format(savings))
    metered_savings = savings.get('metered_savings')
    error_bands = savings.get('error_bands')
    source = "{}:{}".format(model.id, model.model_class)
    if not metered_savings.empty:
        # save the metered savings inot MeterProduction
        logger.info('calc_meter_savings: got metered_savings = {}'.format(metered_savings))
        if progress_observer:
            progress_observer.add_progress(description='Create Meter Productions ...')

        delta = model.get_frequency_delta()

        for d, v in metered_savings.iterrows():
            # logger.info('calc_meter_savings: -> {} = {}'.format(d, v.metered_savings))
            if not isnan(v.metered_savings):
                MeterProduction.objects.create(
                    meter=meter,
                    from_datetime=d,
                    thru_datetime=d + delta,
                    meter_production_type='EEMeter Savings',
                    meter_production_reference={'BaselineModel.id': model.id},
                    error_bands=error_bands,
                    model_baseline_value=v.counterfactual_usage,
                    actual_value=v.reporting_observed,
                    net_value=v.metered_savings,
                    uom=model.uom,
                    source=source)
    model.last_calc_saving_datetime = end
    model.save()
    return model, savings
