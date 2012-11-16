'''
A hypermedia REST API for Salt using the CherryPy framework

:depends:   - CherryPy Python module
:configuration: The master config may contain the following options:

    port
        Required
    debug : ``False``
        Used during development; does not use SSL
    ssl_crt
        Required when ``debug`` is ``False``
    ssl_key
        Required when ``debug`` is ``False``

    For example:

    .. code-block:: yaml

        rest_cherrypy:
          port: 8000
          ssl_crt: /etc/pki/tls/certs/localhost.crt
          ssl_key: /etc/pki/tls/certs/localhost.key

    The REST interface requires a secure HTTPS connection. You must provide an
    SSL certificate to use. If you don't already have a certificate and don't
    wish to buy one, you can generate a self-signed certificate using the
    :py:func:`~salt.modules.tls.create_self_signed_cert` function in Salt (note
    the dependencies for this module):

    .. code-block:: bash

        % salt-call tls.create_self_signed_cert

.. admonition:: Content negotiation

    You may request various output formats by sending the appropriate
    :mailheader:`Accept` header. You may also send various formats in
    :http:method:`post` and :http:method:`put` requests by specifying the
    :mailheader:`Content-Type`. JSON and YAML are currently supported, HTML
    will be soon supported.
'''
# pylint: disable=W0212

# Import Python libs
import itertools
import signal
import os
import json

# Import third-party libs
import cherrypy
import cherrypy.wsgiserver as wsgiserver
import cherrypy.wsgiserver.ssl_builtin

import jinja2

# Import Salt libs
import salt.auth
import salt.log
import salt.output
from salt.utils import yaml

# Import salt-api libs
import saltapi

logger = salt.log.logging.getLogger(__name__)

jenv = jinja2.Environment(loader=jinja2.FileSystemLoader([
    os.path.join(os.path.dirname(__file__), 'tmpl'),
]))

def __virtual__():
    if 'port' in __opts__.get(__name__.rsplit('.')[-1], {}):
        return 'rest'
    return False

def salt_auth_tool():
    '''
    Redirect all unauthenticated requests to the login page. Authentication is
    determined by a session cookie or the custom X-Auth-Token header.
    '''
    ignore_urls = ('/login',)

    # Grab the session via a cookie (for browsers) or via a custom header
    sid = (cherrypy.session.get('token', None) or
            cherrypy.request.headers.get('X-Auth-Token', None))

    if not cherrypy.request.path_info.startswith(ignore_urls) and not sid:
        raise cherrypy.InternalRedirect('/login')

    cherrypy.response.headers['Cache-Control'] = 'private'

# Be conservative in what you send; maps Content-Type to Salt outputters
ct_out_map = {
    'application/json': 'json',
    'application/x-yaml': 'yaml',
}

def hypermedia_handler(*args, **kwargs):
    '''
    Determine the best output format based on the Accept header, execute the
    regular handler, and transform the output to the request content type (even
    if it's an error).
    '''
    try:
        cherrypy.response.processors = ct_out_map # handlers may modify this
        ret = cherrypy.serving.request._hypermedia_inner_handler(*args, **kwargs)
    except cherrypy.CherryPyException:
        raise
    except Exception as exc:
        logger.debug("Error while processing request for: %s",
                cherrypy.request.path_info,
                exc_info=True)

        cherrypy.response.status = 500
        cherrypy.response._tmpl = '500.html'

        ret = {
            'status': cherrypy.response.status,
            'message': '{0}'.format(exc) if cherrypy.config['debug']
                    else "An unexpected error occurred"}

    content_types = cherrypy.response.processors
    best = cherrypy.lib.cptools.accept(content_types.keys()) # raises 406
    cherrypy.response.headers['Content-Type'] = best

    out = content_types[best]

    # Allow handlers to supply the outputter (mostly for the HTML one-offs)
    if callable(out):
        return out(ret)

    return salt.output.out_format(ret, out, __opts__)

def hypermedia_out():
    '''
    Wrap the normal handler and transform the output from that handler into the
    requested content type
    '''
    request = cherrypy.serving.request
    request._hypermedia_inner_handler = request.handler
    request.handler = hypermedia_handler

    # cherrypy.response.headers['Alternates'] = self.ct_out_map.keys()
    # TODO: add 'negotiate' to Vary header and 'list' to TCN header
    # Alternates: {"paper.1" 0.9 {type text/html} {language en}},
    #          {"paper.2" 0.7 {type text/html} {language fr}},
    #          {"paper.3" 1.0 {type application/postscript} {language en}}

def hypermedia_in():
    '''
    Unserialize POST/PUT data of a specified content type, if possible
    '''
    # Be liberal in what you accept
    ct_in_map = {
        'application/x-www-form-urlencoded': cherrypy._cpreqbody.process_urlencoded,
        'application/json': json.loads,
        'application/x-yaml': yaml.load,
        'text/yaml': yaml.load,
    }

    cherrypy.request.body.processors.clear()
    cherrypy.request.body.default_proc = cherrypy.HTTPError(
            406, 'Content type not supported')
    cherrypy.request.body.processors = ct_in_map

class LowDataAdapter(object):
    '''
    The primary purpose of this handler is to provide a RESTful API to execute
    Salt client commands and return the response as a data structure.

    In addition, there is enough functionality to bootstrap the single-page
    browser app (which will then utilize the REST API via ajax calls) when the
    request is intiated from a browser (asks for HTML).
    '''
    exposed = True
    tmpl = 'index.html'

    def __init__(self, opts):
        self.opts = opts
        self.api = saltapi.APIClient(opts)

    def fmt_tmpl(self, data):
        '''
        Allow certain methods in the handler to be able accept requests for
        HTML, then render and return HTML (run through Jinja templates).

        This is intended to allow bootstrapping the web app.
        '''
        cherrypy.response.processors['text/html'] = 'raw'
        tmpl = jenv.get_template(self.tmpl)
        return tmpl.render(data)

    def fmt_lowdata(self, data):
        '''
        Take CherryPy body data from a POST (et al) request and format it into
        lowdata. It will accept repeated parameters and pair and format those
        into multiple lowdata chunks.
        '''
        pairs = []
        for k, v in data.items():
            # Ensure parameter is a list
            argl = v if isinstance(v, list) else [v]
            # Make pairs of (key, value) from {key: [*value]}
            pairs.append(zip([k] * len(argl), argl))

        lowdata = []
        for i in itertools.izip_longest(*pairs):
            if not all(i):
                msg = "Error pairing parameters: %s"
                raise Exception(msg % str(i))
            lowdata.append(dict(i))

        return lowdata

    def exec_lowdata(self, lowdata):
        '''
        Pass lowdata to Salt to be executed
        '''
        logger.debug("SaltAPI is passing low-data: %s", lowdata)
        return [self.api.run(chunk) for chunk in lowdata]

    def GET(self):
        '''
        The API entry point

        .. http:get:: /

            An explanation of the API with links of where to go next.

            **Example request**::

                % curl -i localhost:8000

            .. code-block:: http

                GET / HTTP/1.1
                Host: localhost:8000
                Accept: application/json

            **Example response**:

            .. code-block:: http

                HTTP/1.1 200 OK
                Content-Type: application/json

        :status 200: success
        :status 401: authentication required
        :status 406: requested Content-Type not available
        '''
        cherrypy.response.processors['text/html'] = self.fmt_tmpl

        return {
            'status': cherrypy.response.status,
            'message': "Welcome",
        }

    def POST(self, **kwargs):
        '''
        The primary execution vector for the rest of the API

        .. http:post:: /

            You must pass low-data in the requst body either from an HTML form
            or as JSON or YAML.

            **Example request**::

                % curl -si https://localhost:8000 \\
                        -H "Accept: application/x-yaml" \\
                        -H "X-Auth-Token: d40d1e1e" \\
                        -d client=local \\
                        -d tgt='*' \\
                        -d fun='test.ping' \\
                        -d arg

            .. code-block:: http

                POST / HTTP/1.1
                Host: localhost:8000
                Accept: application/x-yaml
                X-Auth-Token: d40d1e1e
                Content-Length: 36
                Content-Type: application/x-www-form-urlencoded

                fun=test.ping&arg&client=local&tgt=*

            **Example response**:

            .. code-block:: http

                HTTP/1.1 200 OK
                Content-Length: 200
                Allow: GET, HEAD, POST
                Content-Type: application/x-yaml

                return:
                - ms-0: true
                  ms-1: true
                  ms-2: true
                  ms-3: true
                  ms-4: true

        :form client: the client interface in Salt
        :form fun: the function to execute on the specified Salt client
        :form arg: any args to pass to the function; this parameter is required
            even if blank
        :status 200: success
        :status 401: authentication required
        :status 406: requested Content-Type not available
        '''
        return {
            'return': self.exec_lowdata(self.fmt_lowdata(kwargs)),
        }

class Login(LowDataAdapter):
    '''
    All interactions with this REST API must be authenticated. Authentication
    is performed through Salt's eauth system. You must set the eauth backend
    and allowed users by editing the :conf_master:`external_auth` section in
    your master config.

    Authentication credentials are passed to the REST API via a session id in
    one of two ways:

    If the request is initiated from a browser it must pass a session id via a
    cookie and that session must be valid and active.

    If the request is initiated programmatically, the request must contain a
    :mailheader:`X-Auth-Token` header with valid and active session id.
    '''
    exposed = True
    tmpl = 'login.html'

    def GET(self):
        '''
        Present the login interface

        .. http:get:: /login

            An explanation of how to log in.

            **Example request**::

                % curl -i localhost:8000/login

            .. code-block:: http

                GET /login HTTP/1.1
                Host: localhost:8000
                Accept: text/html

            **Example response**:

            .. code-block:: http

                HTTP/1.1 200 OK
                Content-Type: text/html

        :status 401: authentication required
        :status 406: requested Content-Type not available
        '''
        cherrypy.response.processors['text/html'] = self.fmt_tmpl

        cherrypy.response.status = '401 Unauthorized'
        cherrypy.response.headers['WWW-Authenticate'] = 'Session'

        return {
            'status': cherrypy.response.status,
            'message': "Please log in",
        }

    def POST(self, **kwargs):
        '''
        Authenticate against Salt's eauth system. Returns a session id and
        redirects on success.

        .. http:post:: /login

            **Example request**::

                % curl -si localhost:8000/login \\
                        -H "Accept: application/json" \\
                        -d username='saltuser' \\
                        -d password='saltpass' \\
                        -d eauth='pam'

            .. code-block:: http

                POST / HTTP/1.1
                Host: localhost:8000
                Content-Length: 97
                Content-Type: application/x-www-form-urlencoded

                username=saltuser&password=saltpass&eauth=pam

            **Example response**:

            .. code-block:: http

                HTTP/1.1 302 Found
                Content-Length: 97
                Location: http://localhost:8000/
                X-Auth-Token: 6d1b722e
                Set-Cookie: session_id=6d1b722e; expires=Sat, 17 Nov 2012 03:23:52 GMT; Path=/

        :form eauth: the eauth backend configured in your master config
        :form username: username
        :form password: password
        :status 302: success
        :status 406: requested Content-Type not available
        '''
        auth = salt.auth.LoadAuth(self.opts)
        token = auth.mk_token(kwargs).get('token', False)
        cherrypy.response.headers['X-Auth-Token'] = cherrypy.session.id
        cherrypy.session['token'] = token
        raise cherrypy.HTTPRedirect('/', 302)

class API(object):
    '''
    Collect configuration and URL map for building the CherryPy app
    '''
    url_map = {
        'index': LowDataAdapter,
        'login': Login,
    }

    def __init__(self, opts):
        self.opts = opts
        for url, cls in self.url_map.items():
            setattr(self, url, cls(self.opts))

    def verify_certs(self, *args):
        '''
        Sanity checking for the specified SSL certificates
        '''
        msg = ("Could not find a certificate: {0}\n"
                "If you want to quickly generate a self-signed certificate, "
                "use the tls.create_self_signed_cert function in Salt")

        for arg in args:
            if not os.path.exists(arg):
                raise Exception(msg.format(arg))

    def get_conf(self):
        '''
        Combine the CherryPy configuration with config values pulled from the
        master config
        '''
        apiopts = self.opts.get(__name__.rsplit('.', 1)[-1], {})

        conf = {
            'global': {
                'server.socket_host': '0.0.0.0',
                'server.socket_port': apiopts.pop('port', 8000),
                'debug': apiopts.pop('debug', False),
            },
            '/': {
                'request.dispatch': cherrypy.dispatch.MethodDispatcher(),

                'tools.trailing_slash.on': True,
                'tools.gzip.on': True,

                'tools.sessions.on': True,
                'tools.sessions.timeout': 60 * 10, # 10 hours
                'tools.salt_auth.on': True,

                # 'tools.autovary.on': True,
                'tools.hypermedia_out.on': True,
                'tools.hypermedia_in.on': True,
            },
        }

        conf['global'].update(apiopts)
        return conf

def start():
    '''
    Server loop here. Started in a multiprocess.
    '''
    root = API(__opts__)
    conf = root.get_conf()
    gconf = conf.get('global', {})

    cherrypy.tools.salt_auth = cherrypy.Tool('before_request_body', salt_auth_tool)
    cherrypy.tools.hypermedia_out = cherrypy.Tool('before_handler', hypermedia_out)
    cherrypy.tools.hypermedia_in = cherrypy.Tool('before_request_body', hypermedia_in)

    if gconf['debug']:
        cherrypy.quickstart(root, '/', conf)
    else:
        root.verify_certs(gconf['ssl_crt'], gconf['ssl_key'])

        app = cherrypy.tree.mount(root, '/', config=conf)

        ssl_a = wsgiserver.ssl_builtin.BuiltinSSLAdapter(
                gconf['ssl_crt'], gconf['ssl_key'])
        wsgi_d = wsgiserver.WSGIPathInfoDispatcher({'/': app})
        server = wsgiserver.CherryPyWSGIServer(
                ('0.0.0.0', gconf['server.socket_port']),
                wsgi_app=wsgi_d)
        server.ssl_adapter = ssl_a

        signal.signal(signal.SIGINT, lambda *args: server.stop())
        server.start()
