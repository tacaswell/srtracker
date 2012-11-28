# -*- coding: utf-8 -*-

# Copyright (C) 2012, Code for America
# This is open source software, released under a standard 3-clause
# BSD-style license; see the file LICENSE for details.

import os
import datetime
import re
from flask import Flask, render_template, request, abort, redirect, url_for, make_response, session, flash
import requests
import iso8601
import pytz
import updater
from util import bool_from_env
import open311tools

# Config
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'configuration.py')

# Quick-start config. You should really put something in 
# ./configuration.py or set the SRTRACKER_CONFIGURATION env var instead.
DEBUG = True
OPEN311_SERVER = 'http://localhost:5000'
OPEN311_API_KEY = ''
PASSWORD_PROTECTED = False
SECRET_KEY = 'please_please_change_this!'

app = Flask(__name__)

@app.before_request
def password_protect():
    # don't password-protect images (for e-mail!)
    if app.config['PASSWORD_PROTECTED'] and not request.path.startswith('/static/img'):
        auth = request.authorization
        if not auth or auth.password != app.config['PASSWORD']:
            # Tell the browser to do basic auth
            return make_response(
                'Could not verify your access level for that URL.\n'
                'You have to login with proper credentials', 401,
                {'WWW-Authenticate': 'Basic realm="Login Required"'})


#--------------------------------------------------------------------------
# ROUTES
#--------------------------------------------------------------------------

@app.route("/")
def index():
    url = '%s/requests.json' % app.config['OPEN311_SERVER']
    max_recent_srs = app.config.get('MAX_RECENT_SRS', 50)
    recent_sr_timeframe = app.config.get('RECENT_SRS_TIME')

    params = {
        'extensions': 'true',
        'legacy': 'false',
        'page_size': max_recent_srs
    }
    if recent_sr_timeframe:
        start_datetime = datetime.datetime.utcnow() - datetime.timedelta(seconds=recent_sr_timeframe)
        params['start_date'] = start_datetime.isoformat() + 'Z'
    if app.config['OPEN311_API_KEY']:
        params['api_key'] = app.config['OPEN311_API_KEY']

    app.logger.debug('RECENT SRs: %s', params)

    r = requests.get(url, params=params)
    if r.status_code != 200:
        app.logger.error('OPEN311: Failed to load recent requests from Open311 server. Status Code: %s, Response: %s', r.status_code, r.text)
        service_requests = None
    else:
        # need to slice with max_recent_srs in case an endpoint doesn't support page_size
        service_requests = r.json[:max_recent_srs]
    
    return render_app_template('index.html', service_requests=service_requests)


@app.route("/requests/")
def request_search():
    if 'request_id' in request.args:
        return redirect(url_for('show_request', request_id=request.args['request_id']))
    else:
        abort(404)


@app.route("/requests/<request_id>", methods=["GET", "POST"])
def show_request(request_id):
    request_id = request_id.lstrip('#')
    
    # receive subscription
    form_errors = []
    submitted_email = None
    if request.method == 'POST':
        submitted_email = request.form.get('update_email')
        if submitted_email:
            success = subscribe_to_sr(request_id, submitted_email)
            if not success:
                form_errors.append('Please use a valid e-mail address.')
    
    # TODO: Should probably use Three or something nice for this...
    url = '%s/requests/%s.json' % (app.config['OPEN311_SERVER'], request_id)
    params = {'extensions': 'true', 'legacy': 'false'}
    if app.config['OPEN311_API_KEY']:
        params['api_key'] = app.config['OPEN311_API_KEY']
    r = requests.get(url, params=params)
    if r.status_code == 404:
        # TODO: how to generalize this?
        # Chicago's SR IDs are always \d\d-\d{8}, if we get just digits, reformat and try again
        request_id_digits = re.sub(r'\D', '', request_id)
        if len(request_id_digits) == 8:
            # Try prepending the year if it's only 8 digits
            request_id_digits = datetime.date.today().strftime('%y') + request_id_digits
        if len(request_id_digits) == 10:
            reformatted = '%s-%s' % (request_id_digits[:2], request_id_digits[2:])
            if reformatted != request_id:
                return redirect(url_for('show_request', request_id=reformatted))
        
        # It would be nice to log this for analytical purposes (what requests are being checked that we can't show?)
        # but that would be better done through GA or KISS Metrics than through server logging
        services = open311tools.services(app.config['OPEN311_SERVER'], app.config['OPEN311_API_KEY'])
        return render_app_template('error_no_sr.html', request_id=request_id, services=services), 404
        
    elif r.status_code != 200:
        app.logger.error('OPEN311: Error (not 404) loading data for SR %s', request_id)
        return render_app_template('error_311_api.html', request_id=request_id), 500
        
    srs = r.json
    if srs:
        sr = fixup_sr(srs[0], request_id)
        
        if 'requested_datetime' in sr:
            sr['requested_datetime'] = iso8601.parse_date(sr['requested_datetime'])
        
        # sometimes an SR doesn't include notes even though there should always be an "opened" note
        if 'notes' not in sr:
            sr['notes'] = []
        
        relevant_notes = 0
        for note in sr['notes']:
            note['datetime'] = iso8601.parse_date(note['datetime'])
            if note['type'] in ('follow_on', 'follow_on_created', 'activity', 'closed'):
                relevant_notes += 1
        
        # add follow-on closure data, fix types, etc, etc
        by_id = {}

        follow_on_open_count = 0
        follow_on_close_count = 0


        serve_nm_lst =  [s['service_name'] for s in 
                         open311tools.services(app.config['OPEN311_SERVER'], 
                                               app.config['OPEN311_API_KEY'])]

        for note in sr['notes']:
            if note['type'] in ('follow_on', 'follow_on_created', 'follow_on_closed'):
                note_sr_id = note['extended_attributes']['service_request_id']

                
                # old-style is just "follow_on" for everything related to follow-ons
                # new-style is "follow_on_created" and "follow_on_closed"
                # update old notes so templates don't get crazy complicated :(
                if note['type'] == 'follow_on_created' or note['description'].endswith('Created'):
                    note['type'] = 'follow_on_created'
                    follow_on_open_count += 1
                    by_id[note_sr_id] = note

                    serv_nm = note['extended_attributes']['service_name']
                    print serv_nm
                    print open311tools.services(app.config['OPEN311_SERVER'], app.config['OPEN311_API_KEY'])
                    if serv_nm in serve_nm_lst:
                        note['link_url'] = url_for('show_request', request_id=note_sr_id)

                elif note['type'] == 'follow_on_closed' or note['description'].endswith('Closed'):
                    follow_on_close_count += 1
                    note['type'] = 'follow_on_closed'
                    if note_sr_id in by_id:
                        original = by_id[note_sr_id]
                        original['extended_attributes']['closed_datetime'] = note['datetime']

        # if we hit any follow_on_opened notes
        if follow_on_open_count >0:
            # remove the notes that claim the request is closed
            sr['notes'] = [n for n in sr['notes'] if not n['type'] == 'closed']

            # set the request to open
            sr['status'] = 'open'

            # if we hit as many follow_on_closed as follow_on_opened notes, then request is really closed
            if follow_on_open_count == follow_on_close_count:
                # set the request status to closed
                sr['status'] = 'closed'
                tmp_note = {}
                # add a closing note
                tmp_note['type'] = 'closed'
                tmp_note['summary'] = 'Request Completed'
                # this is brittle, but shouldn't break
                tmp_datetime = sorted([n['extended_attributes']['closed_datetime'] for n in by_id.values()])
                # set the closed datetime to be the datetime of the last-closed follow-on
                tmp_note['datetime'] = tmp_datetime[0]
                # add the extra note
                sr['notes'].append(tmp_note)
            

        if 'parent_service_request_id' in sr['extended_attributes']:
            p_req_id = sr['extended_attributes']['parent_service_request_id']
            sr['parent_url'] = url_for('show_request', request_id=p_req_id)


        # if there's no activity yet, show 'under review'
        if relevant_notes == 0:
            sr['notes'].append({
                'type': 'activity',
                'summary': 'Under review by %s staff' % sr.get('agency_responsible', '')
            })
        
        subscribed = False
        if sr['status'] == 'open' and session.get('addr', None):
            # TODO: when subscription service supports more than e-mail, 
            # we should probably be able to show all your subscriptions here
            subscribed = updater.subscription_exists(request_id, 'email', session.get('addr', ''))
            
        # test media
        # sr['media_url'] = sr['media_url'] or 'http://farm5.staticflickr.com/4068/4286605571_c1a1751fdc_n.jpg'
        
        body = render_app_template('service_request.html', sr=sr, subscribed=subscribed, errors=form_errors, submitted_email=submitted_email)
        return (body, 200, None)
    
    else:
        return render_app_template('error_no_sr.html', request_id=request_id), 404


@app.route("/subscribe/<request_id>", methods=["POST"])
def subscribe(request_id):
    email = request.form.get('update_email')
    if email:
        success = subscribe_to_sr(request_id, email)
        if not success:
            flash('Please use a valid e-mail address.', 'error')
    return redirect(url_for('show_request', request_id=request_id))


@app.route("/unsubscribe/<subscription_key>", methods=["GET", "POST"])
def unsubscribe(subscription_key):
    subscription = updater.subscription_for_key(subscription_key)
    if subscription:
        sr_id = subscription.sr_id
        updater.unsubscribe_with_key(subscription_key)
        destination = url_for('show_request', request_id=sr_id)
    else:
        destination = url_for('index')
        
    flash(u'You‘ve been unsubscribed from this service request. You will no longer receive e-mails when it is updated.')
    return redirect(destination)


#--------------------------------------------------------------------------
# ERRORS
#--------------------------------------------------------------------------

@app.errorhandler(404)
def page_not_found(error):
    return render_app_template('error_404.html'), 404


@app.errorhandler(500)
def generic_error(error):
    return render_app_template('error_generic.html'), 500


#--------------------------------------------------------------------------
# FILTERS
#--------------------------------------------------------------------------

# Friendly time by Sean Vieira (http://flask.pocoo.org/snippets/33/)
@app.template_filter()
def friendly_time(dt, past_="ago", future_="from now", default="just now"):
    """
    Returns string representing "time since"
    or "time until" e.g.
    3 days ago, 5 hours from now etc.
    """
    
    if dt == None:
        return ''

    if isinstance(dt, basestring):
        dt = iso8601.parse_date(dt)
        # ensure the date is naive for comparison to utcnow
        if dt.tzinfo:
            dt = dt.astimezone(pytz.utc).replace(tzinfo=None)

    now = datetime.datetime.utcnow()
    if now > dt:
        diff = now - dt
        dt_is_past = True
    else:
        diff = dt - now
        dt_is_past = False

    periods = (
        (diff.days / 365, "year", "years"),
        (diff.days / 30, "month", "months"),
        (diff.days / 7, "week", "weeks"),
        (diff.days, "day", "days"),
        (diff.seconds / 3600, "hour", "hours"),
        (diff.seconds / 60, "minute", "minutes"),
        (diff.seconds, "second", "seconds"),
    )

    for period, singular, plural in periods:

        if period:
            return "%d %s %s" % (period, \
                singular if period == 1 else plural, \
                past_ if dt_is_past else future_)

    return default


state_pattern = re.compile(r'\b(\w\w)(,?\s*\d{5}(?:-\d{4})?)?$')
@app.template_filter()
def title_address(address):
    '''Slightly improved title() method for address strings
    Makes sure state abbreviations are upper-case.'''
    
    titled = address.title()
    titled = state_pattern.sub(lambda match: match.group(1).upper() + (match.group(2) or ''), titled)
    return titled


#--------------------------------------------------------------------------
# UTILITIES
#--------------------------------------------------------------------------

def render_app_template(template, **kwargs):
    '''Add some goodies to all templates.'''

    if 'config' not in kwargs:
        kwargs['config'] = app.config
    return render_template(template, **kwargs)


def fixup_sr(sr, request_id=None):
    '''
    Fix up an SR to try and ensure some basic info.
    (In Chicago's API, any field can be missing, even if it's required.)
    '''
    
    remove_blacklisted_fields(sr)

    if 'service_request_id' not in sr:
        sr['service_request_id'] = request_id or sr.get('token', 'UNKNOWN')
        
    if 'status' not in sr:
        sr['status'] = 'open'
        
    if 'service_name' not in sr:
        sr['service_name'] = 'Miscellaneous Services'
        
    return sr


def remove_blacklisted_fields(sr):
    blacklist = app.config.get('SR_FIELD_BLACKLIST')
    if blacklist:
        for field in blacklist:
            if field in sr:
                del sr[field]


def subscribe_to_sr(request_id, email):
    # validate e-mail
    match = re.match(r'[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,4}$', email, re.IGNORECASE)
    if match:
        key = updater.subscribe(request_id, 'email', email)
        if key:
            # TODO: should we use the subscription key instead?
            session['addr'] = email
            session.permanent = True
            return True
        else:
            app.logger.error('Error creating a subscription for %s on %s', email, request_id)
        
    return False


#--------------------------------------------------------------------------
# INIT
#--------------------------------------------------------------------------

if __name__ == "__main__":
    app.config.from_object(__name__)
    # we want to support a nice fallback, so use from_pyfile directly instead of from_envvar
    config_path = os.path.abspath(os.environ.get('SRTRACKER_CONFIGURATION', DEFAULT_CONFIG_PATH))
    if os.path.isfile(config_path):
        app.config.from_pyfile(config_path)
    else:
        app.logger.warn('''YOU ARE USING THE QUICK-START CONFIG, WHICH IS NOT RECOMMENDED.
            PUT SOMETHING IN "./configuration.py" OR SET THE "SRTRACKER_CONFIGURATION" ENV VAR INSTEAD.''')
    
    port = int(os.environ.get('PORT', 5100))
    app.run(host='0.0.0.0', port=port)
    
