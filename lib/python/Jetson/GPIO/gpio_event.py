# Copyright (c) 2012-2017 Ben Croston <ben@croston.org>.
# Copyright (c) 2019-2022, NVIDIA CORPORATION. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

# Python2 has module thread. Renamed to _thread in Python3
try:
    import thread
except:
    import _thread as thread

import os
import warnings
import fcntl
import ctypes
from Jetson.GPIO import gpio_cdev as cdev
from select import epoll, EPOLLIN, EPOLLET, EPOLLPRI
from datetime import datetime

try:
    InterruptedError = InterruptedError
except:
    InterruptedError = IOError

# sysfs root
#ROOT = "/sys/class/gpio"

# Edge possibilities
NO_EDGE = 0
RISING_EDGE = 1
FALLING_EDGE = 2
BOTH_EDGE = 3

# epoll thread object
_epoll_fd_thread = None

# epoll blocking wait object
_epoll_fd_blocking = None

# 2-layered dictionary of GPIO class objects. key1 = chip name, key2 = channel (pin number by mode)
# value = GPIO class object
_gpio_event_list = {}

# variable to keep track of thread state
_thread_running = False

# string representations for edges to write to sysfs
_edge_str = ["none", "rising", "falling", "both"]

# lock object for thread
_mutex = thread.allocate_lock()

class _Gpios:
    # @edge rising and/or falling edge being monitored
    # @value_fd the file descriptor for the chip line
    # @initial_thread true if the thread just start up (within the first loop)
    # @thread_added the number of threads being added to monitor this object/gpio 
    # @bouncetime the time interval for debouncing
    # @callbacks a list of callback functions to be executed when an edge event happened
    # @lastcall the timestamp for counting debounce
    # @event_occurred  true if an edge event occured
    def __init__(self, line_fd, bouncetime=None):
        self.value_fd = line_fd 
        self.initial_thread = True
        self.thread_added = False
        self.bouncetime = bouncetime
        self.callbacks = []
        self.lastcall = 0
        self.event_occurred = False

    def __del__(self):
        cdev.close_line(self.value_fd)
        del self.callbacks



# @chip_fd file descriptor 
# @channel the file descriptor for the chip line
# @request gpioevent_request struct that describes gpio event monitoring
# @bouncetime the time interval for debouncing
def add_edge_detect(chip_fd, chip_name, channel, request, bouncetime):
    global _epoll_fd_thread
    gpio_obj = None
    res = gpio_event_added(chip_name, channel)

    # event not added
    if not res:
        # open the line
        try:
            fcntl.ioctl(chip_fd, cdev.GPIO_GET_LINEEVENT_IOCTL, request)
        except (OSError, IOError) as e:
            raise cdev.GPIOError(e.errno, "Opening input line event handle: " + e.strerror)
        
        gpio_obj = _Gpios(request.fd, bouncetime)
    else:
        warnings.warn("Warning: event is already added, ignore new added event")
        return 1
    

    # create epoll object for fd if not already open
    if _epoll_fd_thread is None:
        _epoll_fd_thread = epoll()
        if _epoll_fd_thread is None:
            return 2

    # add eventmask and fd to epoll object
    try:
        _epoll_fd_thread.register(gpio_obj.value_fd, EPOLLIN | EPOLLET | EPOLLPRI)
    except IOError:
        remove_edge_detect(chip_name, channel)
        return 2

    gpio_obj.thread_added = 1
    _add_gpio_event(chip_name, channel, gpio_obj) #_gpio_event_list[chip_name][] = gpio_obj

    #WIP: dont know why
    cdev.get_value(request.fd)

    # create and start poll thread if not already running
    if not _thread_running:
        try:
            thread.start_new_thread(_edge_handler, ("edge_handler_thread", request.fd, channel))
        except:
            # remove_edge_detect(gpio, gpio_name)
            warnings.warn("Error: unable to start thread")
            return 2
    return 0


def remove_edge_detect(chip_name, channel):
    if chip_name not in _gpio_event_list:
        return
    if channel not in _gpio_event_list[chip_name]:
        return

    if _epoll_fd_thread is not None:
        _epoll_fd_thread.unregister(_gpio_event_list[chip_name][channel].value_fd)

    _mutex.acquire()
    del _gpio_event_list[chip_name][channel]
    _mutex.release()


def add_edge_callback(chip_name, channel, callback):
    if chip_name not in _gpio_event_list or channel not in _gpio_event_list[chip_name]:
        return 
    if not _gpio_event_list[chip_name][channel].thread_added:
        return 

    _gpio_event_list[chip_name][channel].callbacks.append(callback)


def edge_event_detected(chip_name, channel):
    retval = False
    if chip_name in _gpio_event_list:
        _mutex.acquire()
        if channel in _gpio_event_list[chip_name]:
            if _gpio_event_list[chip_name][channel].event_occurred:
                _gpio_event_list[chip_name][channel].event_occurred = False
                retval = True
        _mutex.release()

    return retval

# @brief Check if any event is added to the channel in the chip controller
# @param[in] chip_name
# @param[in] channel
# @return the gpio object if an event exists, otherwise None
def gpio_event_added(chip_name, channel):
    if chip_name not in _gpio_event_list:
        return None
    if channel not in _gpio_event_list[chip_name]:
        return None
    
    return _gpio_event_list[chip_name][channel]

# @brief Add an event to the channel in the chip controller
# @param[in] chip_name
# @param[in] channel
def _add_gpio_event(chip_name, channel, gpio_obj):
    if chip_name not in _gpio_event_list:
        _gpio_event_list[chip_name] = {}

    if channel not in _gpio_event_list[chip_name]:
        _gpio_event_list[chip_name][channel] = gpio_obj

def _get_gpio_object(gpio):
    raise RuntimeError("This function is deprecated")
    if gpio not in _gpio_event_list:
        return None
    return _gpio_event_list[gpio]


def _set_edge(gpio_name, edge):
    raise RuntimeError("This function is deprecated")
    edge_path = ROOT + "/" + gpio_name + "/edge"

    with open(edge_path, 'w') as edge_file:
        edge_file.write(_edge_str[edge])


def _get_gpio_obj_keys(fd):
    for chip_name in _gpio_event_list:
        for pin in _gpio_event_list[chip_name]:
            if _gpio_event_list[chip_name][pin].value_fd == fd:
                return (chip_name, pin)
    return None, None


def _get_gpio_file_object(fileno):
    raise RuntimeError("This function is deprecated")
    for key in _gpio_event_list:
        if _gpio_event_list[key].value_fd.fileno() == fileno:
            return _gpio_event_list[key].value_fd
    return None

#found that exiting thread should be elegant
#TODO: try epoll to avoid resource busy problem
#TODO: Solve the dual close line problem: check an variableFI
def _edge_handler(thread_name, fd, channel):
    global _thread_running

    _thread_running = True
    while _thread_running:
        try:
            events = _epoll_fd_thread.poll(maxevents=1)
            fd = events[0][0]
            try:
                print("read starting...\n")
                data = os.read(fd, ctypes.sizeof(cdev.gpioevent_data))
                print("read end...", data)

            except OSError as e:
                raise cdev.GPIOError(e.errno, "Reading GPIO event: " + e.strerror)
        
            event_data = cdev.gpioevent_data.from_buffer_copy(data)

            # read file to make sure event is valid
            # TODO
            
            _mutex.acquire()
            # check key to make sure gpio object has not been deleted
            # from main thread
            chip_name, pin_num = _get_gpio_obj_keys(fd)
            if chip_name is None or pin_num is None:
                _thread_running = False
                thread.exit()

            gpio_obj = _gpio_event_list[chip_name][pin_num]

            # event result
            if event_data.id == cdev.GPIOEVENT_REQUEST_RISING_EDGE:
                print("GPIOEVENT_REQUEST_RISING_EDGE")
            elif event_data.id == cdev.GPIOEVENT_REQUEST_FALLING_EDGE:
                print("GPIOEVENT_REQUEST_FALLING_EDGE")
            else:
                print("unknown event")
                continue

            # debounce the input event for the specified bouncetime
            time = datetime.now()
            time = time.second * 1E6 + time.microsecond
            if (gpio_obj.bouncetime is None or
                    (time - gpio_obj.lastcall >
                        gpio_obj.bouncetime * 1000) or
                    (gpio_obj.lastcall == 0) or gpio_obj.lastcall > time):
                gpio_obj.lastcall = time
                gpio_obj.event_occurred = True

                #update to the original list
                _gpio_event_list[chip_name][pin_num] = gpio_obj
                _mutex.release()

                # callback function
                for cb_func in gpio_obj.callbacks:
                    cb_func()
        
        # if interrupted by a signal, continue to start of the loop
        except InterruptedError:
            continue
        except AttributeError:
            break
        finally:
            if _mutex.locked():
                _mutex.release()

    thread.exit()


#WIP: deprecated
def _poll_thread():
    raise RuntimeError("This function is deprecated")

    global _thread_running

    _thread_running = True
    while _thread_running:
        try:
            events = _epoll_fd_thread.poll(maxevents=1)
            fd = events[0][0]
            _mutex.acquire()

            # check if file object has been deleted or closed from main thread
            fd = _get_gpio_file_object(fd)
            if fd is None or fd.closed:
                continue

            # read file to make sure event is valid
            fd.seek(0)
            if len(fd.read().rstrip()) != 1:
                _thread_running = False
                thread.exit()

            # check key to make sure gpio object has not been deleted
            # from main thread
            key = _get_gpio_obj_key(fd)
            if key is None:
                continue

            gpio_obj = _gpio_event_list[key]

            # ignore first epoll trigger
            if gpio_obj.initial_thread:
                gpio_obj.initial_thread = False
                _gpio_event_list[key] = gpio_obj
            else:
                # debounce the input event for the specified bouncetime
                time = datetime.now()
                time = time.second * 1E6 + time.microsecond
                if (gpio_obj.bouncetime is None or
                        (time - gpio_obj.lastcall >
                         gpio_obj.bouncetime * 1000) or
                        (gpio_obj.lastcall == 0) or gpio_obj.lastcall > time):
                    gpio_obj.lastcall = time
                    gpio_obj.event_occurred = True
                    _gpio_event_list[key] = gpio_obj
                    _mutex.release()
                    for cb_func in gpio_obj.callbacks:
                        cb_func()

        # if interrupted by a signal, continue to start of the loop
        except InterruptedError:
            continue
        except AttributeError:
            break
        finally:
            if _mutex.locked():
                _mutex.release()
    thread.exit()

#WIP: deprecated
def blocking_wait_for_edge(gpio, gpio_name, edge, bouncetime, timeout):
    raise RuntimeError("This function is deprecated")

    global _epoll_fd_blocking
    gpio_obj = None
    finished = False
    res = None
    initial_edge = True

    if timeout is None:
        timeout = -1
    else:
        timeout = float(timeout) / 1000

    if gpio in _gpio_event_list:
        if _gpio_event_list[gpio].callbacks:
            return -1

    # check if gpio edge already added. Add if not already added
    added_edge = gpio_event_added(gpio)

    # get existing record
    if added_edge == edge:
        gpio_obj = _get_gpio_object(gpio)
        if (gpio_obj.bouncetime is not None and
                gpio_obj.bouncetime != bouncetime):
            return -1

    # not added. create new record
    elif added_edge == NO_EDGE:
        gpio_obj = _Gpios(gpio_name, edge, bouncetime)
        _set_edge(gpio_name, edge)
        _gpio_event_list[gpio] = gpio_obj

    # added_edge != edge. Event is for different edge
    else:
        _mutex.acquire()
        gpio_obj = _get_gpio_object(gpio)
        _set_edge(gpio_name, edge)
        gpio_obj.edge = edge
        gpio_obj.bouncetime = bouncetime
        gpio_obj.initial_wait = True
        _gpio_event_list[gpio] = gpio_obj
        _mutex.release()

    # create epoll blocking object if not already created
    if _epoll_fd_blocking is None:
        _epoll_fd_blocking = epoll()
        if _epoll_fd_blocking is None:
            return -2

    # register gpio value fd with epoll
    try:
        _epoll_fd_blocking.register(gpio_obj.value_fd, EPOLLIN | EPOLLET |
                                    EPOLLPRI)
    except IOError:
        print("IOError occured while register epoll blocking for GPIO %s"
              % gpio)
        return -2

    while not finished:
        # retry polling if interrupted by signal
        try:
            res = _epoll_fd_blocking.poll(timeout, maxevents=1)
        except InterruptedError:
            continue

        # First trigger is with current state so ignore
        if initial_edge:
            initial_edge = False
            continue

        # debounce input for specified time
        else:
            time = datetime.now()
            time = time.second * 1E6 + time.microsecond
            if ((gpio_obj.bouncetime is None) or
                    (time - gpio_obj.lastcall > gpio_obj.bouncetime * 1000) or
                    (gpio_obj.lastcall == 0) or (gpio_obj.lastcall > time)):
                gpio_obj.lastcall = time
                _mutex.acquire()
                _gpio_event_list[gpio] = gpio_obj
                _mutex.release()
                finished = True

    # check if the event detected was valid
    if res:
        fileno = res[0][0]
        fd = gpio_obj.value_fd
        if fileno != fd.fileno():
            _epoll_fd_blocking.unregister(gpio_obj.value_fd)
            print("File object not found after wait for GPIO %s" % gpio)
            return -2
        else:
            _mutex.acquire()
            fd.seek(0)
            value_str = fd.read().rstrip()
            _mutex.release()
            if len(value_str) != 1:
                _epoll_fd_blocking.unregister(gpio_obj.value_fd)
                print("Length of value string was not 1 for GPIO %s" % gpio)
                return -2

    _epoll_fd_blocking.unregister(gpio_obj.value_fd)

    # 0 if timeout occured - res == []
    # 1 if event was valid
    return int(res != [])

#wip back
def event_cleanup(chip_name, channel):
    global _epoll_fd_thread, _epoll_fd_blocking, _thread_running

    _thread_running = False
    if chip_name in _gpio_event_list and channel in _gpio_event_list[chip_name]:
        remove_edge_detect(chip_name, channel)

    if _gpio_event_list == {} or (chip_name in _gpio_event_list and _gpio_event_list[chip_name] == {}):
        if _epoll_fd_blocking is not None:
            _epoll_fd_blocking.close()
            _epoll_fd_blocking = None

        if _epoll_fd_thread is not None:
            _epoll_fd_thread.close()
            _epoll_fd_thread = None
