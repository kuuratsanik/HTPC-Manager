#!/usr/bin/python
# -- Content-Encoding: UTF-8 --
"""
============================
JSONRPC Library (jsonrpclib)
============================

This library is a JSON-RPC v.2 (proposed) implementation which
follows the xmlrpclib API for portability between clients. It
uses the same Server / ServerProxy, loads, dumps, etc. syntax,
while providing features not present in XML-RPC like:

* Keyword arguments
* Notifications
* Versioning
* Batches and batch notifications

Eventually, I'll add a SimpleXMLRPCServer compatible library,
and other things to tie the thing off nicely. :)

For a quick-start, just open a console and type the following,
replacing the server address, method, and parameters
appropriately.
>>> import jsonrpclib
>>> server = jsonrpclib.Server('http://localhost:8181')
>>> server.add(5, 6)
11
>>> server._notify.add(5, 6)
>>> batch = jsonrpclib.MultiCall(server)
>>> batch.add(3, 50)
>>> batch.add(2, 3)
>>> batch._notify.add(3, 5)
>>> batch()
[53, 5]

See https://github.com/tcalmant/jsonrpclib for more info.

:authors: Josh Marshall, Thomas Calmant
:copyright: Copyright 2015, isandlaTech
:license: Apache License 2.0
:version: 0.2.4

..

    Copyright 2015 isandlaTech

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.
"""

# Module version
__version_info__ = (0, 2, 4)
__version__ = ".".join(str(x) for x in __version_info__)

# Documentation strings format
__docformat__ = "restructuredtext en"

# ------------------------------------------------------------------------------

# Library includes
import jsonrpclib.config
import jsonrpclib.utils as utils

# Standard library
import contextlib
import logging
import sys
import uuid

# Create the logger
_logger = logging.getLogger(__name__)

try:
    # Python 3
    # pylint: disable=F0401,E0611
    from urllib.parse import splittype
    from urllib.parse import splithost
    from xmlrpc.client import Transport as XMLTransport
    from xmlrpc.client import SafeTransport as XMLSafeTransport
    from xmlrpc.client import ServerProxy as XMLServerProxy
    from xmlrpc.client import _Method as XML_Method

except ImportError:
    # Python 2
    # pylint: disable=F0401,E0611
    from urllib import splittype
    from urllib import splithost
    from xmlrpclib import Transport as XMLTransport
    from xmlrpclib import SafeTransport as XMLSafeTransport
    from xmlrpclib import ServerProxy as XMLServerProxy
    from xmlrpclib import _Method as XML_Method

# ------------------------------------------------------------------------------
# JSON library import

# JSON class serialization
from jsonrpclib import jsonclass

try:
    # pylint: disable=F0401,E0611
    # Using cjson
    import cjson
    _logger.debug("Using cjson as JSON library")

    # Declare cjson methods
    def jdumps(obj, encoding='utf-8'):
        """
        Serializes ``obj`` to a JSON formatted string, using cjson.
        """
        return cjson.encode(obj)

    def jloads(json_string):
        """
        Deserializes ``json_string`` (a string containing a JSON document)
        to a Python object, using cjson.
        """
        return cjson.decode(json_string)

except ImportError:
    # pylint: disable=F0401,E0611
    # Use json or simplejson
    try:
        import json
        _logger.debug("Using json as JSON library")

    except ImportError:
        try:
            import simplejson as json
            _logger.debug("Using simplejson as JSON library")
        except ImportError:
            _logger.error("No supported JSON library found")
            raise ImportError('You must have the cjson, json, or simplejson '
                              'module(s) available.')

    # Declare json methods
    if sys.version_info[0] < 3:
        def jdumps(obj, encoding='utf-8'):
            """
            Serializes ``obj`` to a JSON formatted string.
            """
            # Python 2 (explicit encoding)
            return json.dumps(obj, encoding=encoding)

    else:
        # Python 3
        def jdumps(obj, encoding='utf-8'):
            """
            Serializes ``obj`` to a JSON formatted string.
            """
            # Python 3 (the encoding parameter has been removed)
            return json.dumps(obj)

    def jloads(json_string):
        """
        Deserializes ``json_string`` (a string containing a JSON document)
        to a Python object.
        """
        return json.loads(json_string)

# ------------------------------------------------------------------------------
# XMLRPClib re-implementations


class ProtocolError(Exception):
    """
    JSON-RPC error

    ProtocolError.args[0] can be:
    * an error message (string)
    * a (code, message) tuple
    """
    pass


class AppError(ProtocolError):
    """
    Application error: the error code is not in the pre-defined ones

    AppError.args[0][0]: Error code
    AppError.args[0][1]: Error message or trace
    AppError.args[0][2]: Associated data
    """
    def data(self):
        """
        Retrieves the value found in the 'data' entry of the error, or None

        :return: The data associated to the error, or None
        """
        return self.args[0][2]


class JSONParser(object):
    """
    Default JSON parser
    """
    def __init__(self, target):
        """
        Associates the target loader to the parser

        :param target: a JSONTarget instance
        """
        self.target = target

    def feed(self, data):
        """
        Feeds the associated target with the given data
        """
        self.target.feed(data)

    def close(self):
        """
        Does nothing
        """
        pass


class JSONTarget(object):
    """
    Unmarshalls stream data to a string
    """
    def __init__(self):
        """
        Sets up the unmarshaller
        """
        self.data = []

    def feed(self, data):
        """
        Stores the given raw data into a buffer
        """
        # Store raw data as it might not contain whole wide-character
        self.data.append(data)

    def close(self):
        """
        Unmarshalls the buffered data
        """
        if not self.data:
            return ''
        else:
            # Use type to have a valid join (str vs. bytes)
            data = type(self.data[0])().join(self.data)
            try:
                # Convert the whole final string
                data = utils.from_bytes(data)
            except:
                # Try a pass-through
                pass

            return data


class TransportMixIn(object):
    """ Just extends the XMLRPC transport where necessary. """
    # for Python 2.7 support
    _connection = None

    # List of non-overridable headers
    # Use the configuration to change the content-type
    readonly_headers = ('content-length', 'content-type')

    def __init__(self, config=jsonrpclib.config.DEFAULT, context=None):
        """
        Sets up the transport

        :param config: A JSONRPClib Config instance
        """
        # Store the configuration
        self._config = config

        # Store the SSL context
        self.context = context

        # Set up the user agent
        self.user_agent = config.user_agent

        # Additional headers: list of dictionaries
        self.additional_headers = []

    def push_headers(self, headers):
        """
        Adds a dictionary of headers to the additional headers list

        :param headers: A dictionary
        """
        self.additional_headers.append(headers)

    def pop_headers(self, headers):
        """
        Removes the given dictionary from the additional headers list.
        Also validates that given headers are on top of the stack

        :param headers: Headers to remove
        :raise AssertionError: The given dictionary is not on the latest stored
                               in the additional headers list
        """
        assert self.additional_headers[-1] == headers
        self.additional_headers.pop()

    def emit_additional_headers(self, connection):
        """
        Puts headers as is in the request, filtered read only headers

        :param connection: The request connection
        """
        additional_headers = {}

        # Prepare the merged dictionary
        for headers in self.additional_headers:
            additional_headers.update(headers)

        # Remove forbidden keys
        for forbidden in self.readonly_headers:
            additional_headers.pop(forbidden, None)

        # Reversed order: in the case of multiple headers value definition,
        # the latest pushed has priority
        for key, value in additional_headers.items():
            key = str(key)
            if key.lower() not in self.readonly_headers:
                # Only accept replaceable headers
                connection.putheader(str(key), str(value))

    def send_content(self, connection, request_body):
        """
        Completes the request headers and sends the request body of a JSON-RPC
        request over a HTTPConnection

        :param connection: An HTTPConnection object
        :param request_body: JSON-RPC request body
        """
        # Convert the body first
        request_body = utils.to_bytes(request_body)

        # "static" headers
        connection.putheader("Content-Type", self._config.content_type)
        connection.putheader("Content-Length", str(len(request_body)))

        # Emit additional headers here in order not to override content-length
        self.emit_additional_headers(connection)

        connection.endheaders()
        if request_body:
            connection.send(request_body)

    def getparser(self):
        """
        Create an instance of the parser, and attach it to an unmarshalling
        object. Return both objects.

        :return: The parser and unmarshaller instances
        """
        target = JSONTarget()
        return JSONParser(target), target


class Transport(TransportMixIn, XMLTransport):
    """
    Mixed-in HTTP transport
    """
    pass


class SafeTransport(TransportMixIn, XMLSafeTransport):
    """
    Mixed-in HTTPS transport
    """
    pass

# ------------------------------------------------------------------------------


class ServerProxy(XMLServerProxy):
    """
    Unfortunately, much more of this class has to be copied since
    so much of it does the serialization.
    """
    def __init__(self, uri, transport=None, encoding=None,
                 verbose=0, version=None, headers=None, history=None,
                 config=jsonrpclib.config.DEFAULT, context=None):
        """
        Sets up the server proxy

        :param uri: Request URI
        :param transport: Custom transport handler
        :param encoding: Specified encoding
        :param verbose: Log verbosity level
        :param version: JSON-RPC specification version
        :param headers: Custom additional headers for each request
        :param history: History object (for tests)
        :param config: A JSONRPClib Config instance
        :param context: The optional SSLContext to use
        """
        # Store the configuration
        self._config = config
        self.__version = version or config.version

        schema, uri = splittype(uri)
        if schema not in ('http', 'https'):
            _logger.error("jsonrpclib only support http(s) URIs, not %s",
                          schema)
            raise IOError('Unsupported JSON-RPC protocol.')

        self.__host, self.__handler = splithost(uri)
        if not self.__handler:
            # Not sure if this is in the JSON spec?
            self.__handler = '/'

        if transport is None:
            if schema == 'https':
                transport = SafeTransport(config=config, context=context)
            else:
                transport = Transport(config=config)
        self.__transport = transport

        self.__encoding = encoding
        self.__verbose = verbose
        self.__history = history

        # Global custom headers are injected into Transport
        self.__transport.push_headers(headers or {})

    def _request(self, methodname, params, rpcid=None):
        """
        Calls a method on the remote server

        :param methodname: Name of the method to call
        :param params: Method parameters
        :param rpcid: ID of the remote call
        :return: The parsed result of the call
        """
        request = dumps(params, methodname, encoding=self.__encoding,
                        rpcid=rpcid, version=self.__version,
                        config=self._config)
        response = self._run_request(request)
        check_for_errors(response)
        return response['result']

    def _request_notify(self, methodname, params, rpcid=None):
        """
        Calls a method as a notification

        :param methodname: Name of the method to call
        :param params: Method parameters
        :param rpcid: ID of the remote call
        """
        request = dumps(params, methodname, encoding=self.__encoding,
                        rpcid=rpcid, version=self.__version, notify=True,
                        config=self._config)
        response = self._run_request(request, notify=True)
        check_for_errors(response)

    def _run_request(self, request, notify=False):
        """
        Sends the given request to the remote server

        :param request: The request to send
        :param notify: Notification request flag (unused)
        :return: The response as a parsed JSON object
        """
        if self.__history is not None:
            self.__history.add_request(request)

        response = self.__transport.request(
            self.__host,
            self.__handler,
            request,
            verbose=self.__verbose
        )

        # Here, the XMLRPC library translates a single list
        # response to the single value -- should we do the
        # same, and require a tuple / list to be passed to
        # the response object, or expect the Server to be
        # outputting the response appropriately?

        if self.__history is not None:
            self.__history.add_response(response)

        if not response:
            return None
        else:
            return_obj = loads(response, self._config)
            return return_obj

    def __getattr__(self, name):
        """
        Returns a callable object to call the remote service
        """
        # Same as original, just with new _Method reference
        return _Method(self._request, name)

    def __close(self):
        """
        Closes the transport layer
        """
        try:
            self.__transport.close()
        except AttributeError:
            # Not available in Python 2.6
            pass

    def __call__(self, attr):
        """
        A workaround to get special attributes on the ServerProxy
        without interfering with the magic __getattr__

        (code from xmlrpclib in Python 2.7)
        """
        if attr == "close":
            return self.__close

        elif attr == "transport":
            return self.__transport

        raise AttributeError("Attribute {0} not found".format(attr))

    @property
    def _notify(self):
        """
        Like __getattr__, but sending a notification request instead of a call
        """
        return _Notify(self._request_notify)

    @contextlib.contextmanager
    def _additional_headers(self, headers):
        """
        Allows to specify additional headers, to be added inside the with
        block.
        Example of usage:

        >>> with client._additional_headers({'X-Test' : 'Test'}) as new_client:
        ...     new_client.method()
        ...
        >>> # Here old headers are restored
        """
        self.__transport.push_headers(headers)
        yield self
        self.__transport.pop_headers(headers)

# ------------------------------------------------------------------------------


class _Method(XML_Method):
    """
    Some magic to bind an JSON-RPC method to an RPC server.
    """
    def __call__(self, *args, **kwargs):
        """
        Sends an RPC request and returns the unmarshalled result
        """
        if args and kwargs:
            raise ProtocolError("Cannot use both positional and keyword "
                                "arguments (according to JSON-RPC spec.)")
        if args:
            return self.__send(self.__name, args)
        else:
            return self.__send(self.__name, kwargs)

    def __getattr__(self, name):
        """
        Returns a Method object for nested calls
        """
        if name == "__name__":
            return self.__name
        return _Method(self.__send, "{0}.{1}".format(self.__name, name))


class _Notify(object):
    """
    Same as _Method, but to send notifications
    """
    def __init__(self, request):
        """
        Sets the method to call to send a request to the server
        """
        self._request = request

    def __getattr__(self, name):
        """
        Returns a Method object, to be called as a notification
        """
        return _Method(self._request, name)

# ------------------------------------------------------------------------------
# Batch implementation


class MultiCallMethod(object):
    """
    Stores calls made to a MultiCall object for batch execution
    """
    def __init__(self, method, notify=False, config=jsonrpclib.config.DEFAULT):
        """
        Sets up the store

        :param method: Name of the method to call
        :param notify: Notification flag
        :param config: Request configuration
        """
        self.method = method
        self.params = []
        self.notify = notify
        self._config = config

    def __call__(self, *args, **kwargs):
        """
        Normalizes call parameters
        """
        if kwargs and args:
            raise ProtocolError('JSON-RPC does not support both ' +
                                'positional and keyword arguments.')
        if kwargs:
            self.params = kwargs
        else:
            self.params = args

    def request(self, encoding=None, rpcid=None):
        """
        Returns the request object as JSON-formatted string
        """
        return dumps(self.params, self.method, version=2.0,
                     encoding=encoding, rpcid=rpcid, notify=self.notify,
                     config=self._config)

    def __repr__(self):
        """
        String representation
        """
        return str(self.request())

    def __getattr__(self, method):
        """
        Updates the object for a nested call
        """
        self.method = "{0}.{1}".format(self.method, method)
        return self


class MultiCallNotify(object):
    """
    Same as MultiCallMethod but for notifications
    """
    def __init__(self, multicall, config=jsonrpclib.config.DEFAULT):
        """
        Sets ip the store

        :param multicall: The parent MultiCall instance
        :param config: Request configuration
        """
        self.multicall = multicall
        self._config = config

    def __getattr__(self, name):
        """
        Returns the MultiCallMethod to use as a notification
        """
        new_job = MultiCallMethod(name, notify=True, config=self._config)
        self.multicall._job_list.append(new_job)
        return new_job


class MultiCallIterator(object):
    """
    Iterates over the results of a MultiCall.
    Exceptions are raised in response to JSON-RPC faults
    """
    def __init__(self, results):
        """
        Sets up the results store
        """
        self.results = results

    def __get_result(self, item):
        """
        Checks for error and returns the "real" result stored in a MultiCall
        result.
        """
        check_for_errors(item)
        return item['result']

    def __iter__(self):
        """
        Iterates over all results
        """
        for item in self.results:
            yield self.__get_result(item)
        raise StopIteration

    def __getitem__(self, i):
        """
        Returns the i-th object of the results
        """
        return self.__get_result(self.results[i])

    def __len__(self):
        """
        Returns the number of results stored
        """
        return len(self.results)


class MultiCall(object):
    """
    server -> a object used to boxcar method calls, where server should be a
    ServerProxy object.

    Methods can be added to the MultiCall using normal
    method call syntax e.g.:

    multicall = MultiCall(server_proxy)
    multicall.add(2,3)
    multicall.get_address("Guido")

    To execute the multicall, call the MultiCall object e.g.:

    add_result, address = multicall()
    """
    def __init__(self, server, config=jsonrpclib.config.DEFAULT):
        """
        Sets up the multicall

        :param server: A ServerProxy object
        :param config: Request configuration
        """
        self._server = server
        self._job_list = []
        self._config = config

    def _request(self):
        """
        Sends the request to the server and returns the responses

        :return: A MultiCallIterator object
        """
        if len(self._job_list) < 1:
            # Should we alert? This /is/ pretty obvious.
            return
        request_body = "[ {0} ]".format(
            ','.join(job.request() for job in self._job_list))
        responses = self._server._run_request(request_body)
        del self._job_list[:]
        if not responses:
            responses = []
        return MultiCallIterator(responses)

    @property
    def _notify(self):
        """
        Prepares a notification call
        """
        return MultiCallNotify(self, self._config)

    def __getattr__(self, name):
        """
        Registers a method call
        """
        new_job = MultiCallMethod(name, config=self._config)
        self._job_list.append(new_job)
        return new_job

    __call__ = _request

# These lines conform to xmlrpclib's "compatibility" line.
# Not really sure if we should include these, but oh well.
Server = ServerProxy

# ------------------------------------------------------------------------------


class Fault(object):
    """
    JSON-RPC error class
    """
    def __init__(self, code=-32000, message='Server error', rpcid=None,
                 config=jsonrpclib.config.DEFAULT, data=None):
        """
        Sets up the error description

        :param code: Fault code
        :param message: Associated message
        :param rpcid: Request ID
        :param config: A JSONRPClib Config instance
        :param data: Extra information added to an error description
        """
        self.faultCode = code
        self.faultString = message
        self.rpcid = rpcid
        self.config = config
        self.data = data

    def error(self):
        """
        Returns the error as a dictionary

        :returns: A {'code', 'message'} dictionary
        """
        return {'code': self.faultCode, 'message': self.faultString,
                'data': self.data}

    def response(self, rpcid=None, version=None):
        """
        Returns the error as a JSON-RPC response string

        :param rpcid: Forced request ID
        :param version: JSON-RPC version
        :return: A JSON-RPC response string
        """
        if not version:
            version = self.config.version

        if rpcid:
            self.rpcid = rpcid

        return dumps(self, methodresponse=True, rpcid=self.rpcid,
                     version=version, config=self.config)

    def dump(self, rpcid=None, version=None):
        """
        Returns the error as a JSON-RPC response dictionary

        :param rpcid: Forced request ID
        :param version: JSON-RPC version
        :return: A JSON-RPC response dictionary
        """
        if not version:
            version = self.config.version

        if rpcid:
            self.rpcid = rpcid

        return dump(self, is_response=True, rpcid=self.rpcid,
                    version=version, config=self.config)

    def __repr__(self):
        """
        String representation
        """
        return '<Fault {0}: {1}>'.format(self.faultCode, self.faultString)


class Payload(object):
    """
    JSON-RPC content handler
    """
    def __init__(self, rpcid=None, version=None,
                 config=jsonrpclib.config.DEFAULT):
        """
        Sets up the JSON-RPC handler

        :param rpcid: Request ID
        :param version: JSON-RPC version
        :param config: A JSONRPClib Config instance
        """
        if not version:
            version = config.version

        self.id = rpcid
        self.version = float(version)

    def request(self, method, params=None):
        """
        Prepares a method call request

        :param method: Method name
        :param params: Method parameters
        :return: A JSON-RPC request dictionary
        """
        if not isinstance(method, utils.string_types):
            raise ValueError('Method name must be a string.')

        if not self.id:
            # Generate a request ID
            self.id = str(uuid.uuid4())

        request = {'id': self.id, 'method': method}
        if params or self.version < 1.1:
            request['params'] = params or []

        if self.version >= 2:
            request['jsonrpc'] = str(self.version)

        return request

    def notify(self, method, params=None):
        """
        Prepares a notification request

        :param method: Notification name
        :param params: Notification parameters
        :return: A JSON-RPC notification dictionary
        """
        # Prepare the request dictionary
        request = self.request(method, params)

        # Remove the request ID, as it's a notification
        if self.version >= 2:
            del request['id']
        else:
            request['id'] = None

        return request

    def response(self, result=None):
        """
        Prepares a response dictionary

        :param result: The result of method call
        :return: A JSON-RPC response dictionary
        """
        response = {'result': result, 'id': self.id}

        if self.version >= 2:
            response['jsonrpc'] = str(self.version)
        else:
            response['error'] = None

        return response

    def error(self, code=-32000, message='Server error.', data=None):
        """
        Prepares an error dictionary

        :param code: Error code
        :param message: Error message
        :return: A JSON-RPC error dictionary
        """
        error = self.response()
        if self.version >= 2:
            del error['result']
        else:
            error['result'] = None
        error['error'] = {'code': code, 'message': message}
        if data is not None:
            error['error']['data'] = data
        return error

# ------------------------------------------------------------------------------


def dump(params=None, methodname=None, rpcid=None, version=None,
         is_response=None, is_notify=None, config=jsonrpclib.config.DEFAULT):
    """
    Prepares a JSON-RPC dictionary (request, notification, response or error)

    :param params: Method parameters (if a method name is given) or a Fault
    :param methodname: Method name
    :param rpcid: Request ID
    :param version: JSON-RPC version
    :param is_response: If True, this is a response dictionary
    :param is_notify: If True, this is a notification request
    :param config: A JSONRPClib Config instance
    :return: A JSON-RPC dictionary
    """
    # Default version
    if not version:
        version = config.version

    if not is_response and params is None:
        params = []

    # Validate method name and parameters
    valid_params = [utils.TupleType, utils.ListType, utils.DictType, Fault]
    if is_response:
        valid_params.append(type(None))

    if isinstance(methodname, utils.string_types) and \
            not isinstance(params, tuple(valid_params)):
        """
        If a method, and params are not in a listish or a Fault,
        error out.
        """
        raise TypeError("Params must be a dict, list, tuple "
                        "or Fault instance.")

    # Prepares the JSON-RPC content
    payload = Payload(rpcid=rpcid, version=version)

    if isinstance(params, Fault):
        # Prepare an error dictionary
        # pylint: disable=E1103
        return payload.error(params.faultCode, params.faultString, params.data)

    if not isinstance(methodname, utils.string_types) and not is_response:
        # Neither a request nor a response
        raise ValueError('Method name must be a string, or is_response '
                         'must be set to True.')

    if config.use_jsonclass:
        # Use jsonclass to convert the parameters
        params = jsonclass.dump(params, config=config)

    if is_response:
        # Prepare a response dictionary
        if rpcid is None:
            # A response must have a request ID
            raise ValueError('A method response must have an rpcid.')
        return payload.response(params)

    if is_notify:
        # Prepare a notification dictionary
        return payload.notify(methodname, params)
    else:
        # Prepare a method call dictionary
        return payload.request(methodname, params)


def dumps(params=None, methodname=None, methodresponse=None,
          encoding=None, rpcid=None, version=None, notify=None,
          config=jsonrpclib.config.DEFAULT):
    """
    Prepares a JSON-RPC request/response string

    :param params: Method parameters (if a method name is given) or a Fault
    :param methodname: Method name
    :param methodresponse: If True, this is a response dictionary
    :param encoding: Result string encoding
    :param rpcid: Request ID
    :param version: JSON-RPC version
    :param notify: If True, this is a notification request
    :param config: A JSONRPClib Config instance
    :return: A JSON-RPC dictionary
    """
    # Prepare the dictionary
    request = dump(params, methodname, rpcid, version, methodresponse, notify,
                   config)

    # Returns it as a JSON string
    return jdumps(request, encoding=encoding or "UTF-8")


def load(data, config=jsonrpclib.config.DEFAULT):
    """
    Loads a JSON-RPC request/response dictionary. Calls jsonclass to load beans

    :param data: A JSON-RPC dictionary
    :param config: A JSONRPClib Config instance (or None for default values)
    :return: A parsed dictionary or None
    """
    if data is None:
        # Notification
        return None

    # if the above raises an error, the implementing server code
    # should return something like the following:
    # { 'jsonrpc':'2.0', 'error': fault.error(), id: None }
    if config.use_jsonclass:
        # Convert beans
        data = jsonclass.load(data, config.classes)

    return data


def loads(data, config=jsonrpclib.config.DEFAULT):
    """
    Loads a JSON-RPC request/response string. Calls jsonclass to load beans

    :param data: A JSON-RPC string
    :param config: A JSONRPClib Config instance (or None for default values)
    :return: A parsed dictionary or None
    """
    if data == '':
        # Notification
        return None

    # Parse the JSON dictionary
    result = jloads(data)

    # Load the beans
    return load(result, config)

# ------------------------------------------------------------------------------


def check_for_errors(result):
    """
    Checks if a result dictionary signals an error

    :param result: A result dictionary
    :raise TypeError: Invalid parameter
    :raise NotImplementedError: Unknown JSON-RPC version
    :raise ValueError: Invalid dictionary content
    :raise ProtocolError: An error occurred on the server side
    :return: The result parameter
    """
    if not result:
        # Notification
        return result

    if not isinstance(result, utils.DictType):
        # Invalid argument
        raise TypeError('Response is not a dict.')

    if 'jsonrpc' in result and float(result['jsonrpc']) > 2.0:
        # Unknown JSON-RPC version
        raise NotImplementedError('JSON-RPC version not yet supported.')

    if 'result' not in result and 'error' not in result:
        # Invalid dictionary content
        raise ValueError('Response does not have a result or error key.')

    if 'error' in result and result['error']:
        # Server-side error
        if 'code' in result['error']:
            # Code + Message
            code = result['error']['code']
            try:
                # Get the message (jsonrpclib)
                message = result['error']['message']
            except KeyError:
                # Get the trace (jabsorb)
                message = result['error'].get('trace', '<no error message>')

            if -32700 <= code <= -32000:
                # Pre-defined errors
                # See http://www.jsonrpc.org/specification#error_object
                raise ProtocolError((code, message))
            else:
                # Application error
                data = result['error'].get('data', None)
                raise AppError((code, message, data))

        elif isinstance(result['error'], dict) and len(result['error']) == 1:
            # Error with a single entry ('reason', ...): use its content
            error_key = result['error'].keys()[0]
            raise ProtocolError(result['error'][error_key])

        else:
            # Use the raw error content
            raise ProtocolError(result['error'])

    return result


def isbatch(request):
    """
    Tests if the given request is a batch call, i.e. a list of multiple calls
    :param request: a JSON-RPC request object
    :return: True if the request is a batch call
    """
    if not isinstance(request, (utils.ListType, utils.TupleType)):
        # Not a list: not a batch call
        return False
    elif len(request) < 1:
        # Only one request: not a batch call
        return False
    elif not isinstance(request[0], utils.DictType):
        # One of the requests is not a dictionary, i.e. a JSON Object
        # therefore it is not a valid JSON-RPC request
        return False
    elif 'jsonrpc' not in request[0].keys():
        # No "jsonrpc" version in the JSON object: not a request
        return False

    try:
        version = float(request[0]['jsonrpc'])
    except ValueError:
        # Bad version of JSON-RPC
        raise ProtocolError('"jsonrpc" key must be a float(able) value.')

    if version < 2:
        # Batch call were not supported before JSON-RPC 2.0
        return False

    return True


def isnotification(request):
    """
    Tests if the given request is a notification

    :param request: A request dictionary
    :return: True if the request is a notification
    """
    if 'id' not in request:
        # 2.0 notification
        return True

    if request['id'] is None:
        # 1.0 notification
        return True

    return False
