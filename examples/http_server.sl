# A small HTTP/1.1 server written in plain L2 on raw Linux syscalls.
#
#   python3 main.py examples/http_server.sl -o http_server
#   ./http_server            # listens on 127.0.0.1:8080
#   ./http_server 9000       # or on another port
#
# Routes:
#   GET /          an HTML page linking the other routes
#   GET /hello     plain text greeting
#   GET /stats     number of requests served so far (built at runtime)
#   GET /echo      sends the raw request back
#   GET /shutdown  stops the server
#   anything else  404 Not Found
#
# HEAD gets the same headers as GET without the body; other methods get 405.
#
# One connection is handled at a time; every response closes the connection.

import stdlib.sl
import linux.sl
import gvars.sl

const DEFAULT_PORT 8080
const BACKLOG 16
const REQ_CAP 8192
const OUT_CAP 8192
const SOL_SOCKET 1
const SO_REUSEADDR 2
const CH_SPACE 32
const CH_QUESTION 63

global server_fd
global client_fd
global port
global running
global head_only
global served
global req_len
global path_addr
global path_len
global out_len
sized_global req_buf 8192
sized_global out_buf 8192
sized_global sockaddr 16
sized_global optval 8
sized_global digit 8

# ---------------------------------------------------------------------------
#  Output to the client
# ---------------------------------------------------------------------------

#send_all [*, addr | len] -> [*]
# Writes the whole buffer to the client, retrying after partial writes.
word send_all
    while dup 0 > do
        client_fd @ 2 pick 2 pick syscall.write   # addr len written
        dup 0 <= if drop break end
        tuck - -rot + swap
    end
    2drop
end

#send_num [* | n] -> [*]
# Writes a non-negative integer in decimal.
word send_num
    dup 10 >= if dup 10 / send_num end
    10 % 48 + digit swap c!
    digit 1 send_all
end

#respond_headers [*, status_addr, status_len, type_addr, type_len | body_len] -> [*]
# The status line and the common headers; the caller ends the header block.
word respond_headers
    >r                                          # status type     R: body_len
    "HTTP/1.1 " send_all
    2swap send_all                              # type
    "\r\nContent-Type: " send_all send_all
    "\r\nContent-Length: " send_all
    r> send_num
    "\r\nServer: l2\r\nConnection: close\r\n" send_all
end

#respond [*, status_addr, status_len, type_addr, type_len, body_addr | body_len] -> [*]
# The body is left out for a HEAD request.
word respond
    >r >r                                       # status type     R: body_len body_addr
    1 rpick respond_headers
    "\r\n" send_all
    r> r>                                       # body_addr body_len
    head_only @ if 2drop else send_all end
end

word method_not_allowed
    "405 Method Not Allowed" "text/plain" "only GET and HEAD are supported\n"
    >r >r
    1 rpick respond_headers
    "Allow: GET, HEAD\r\n\r\n" send_all
    r> r> send_all
end

# ---------------------------------------------------------------------------
#  A response body built at runtime
# ---------------------------------------------------------------------------

word out_reset  out_len 0 ! end

#out_str [*, addr | len] -> [*]
word out_str
    dup out_len @ + OUT_CAP > if 2drop else
        out_buf out_len @ + -rot                # dst src len
        dup out_len @ + out_len swap !
        memcpy 2drop
    end
end

#out_num [* | n] -> [*]
word out_num
    dup 10 >= if dup 10 / out_num end
    10 % 48 + digit swap c!
    digit 1 out_str
end

# ---------------------------------------------------------------------------
#  Routes
# ---------------------------------------------------------------------------

#path= [*, addr | len] -> [* | flag]
word path=
    path_addr @ path_len @ strcmp
end

word index_page
"<!doctype html>
<html><head><title>L2 HTTP server</title></head>
<body style=\"font-family: sans-serif; max-width: 40em; margin: 3em auto\">
<h1>Hello from L2</h1>
<p>This page is served by a native x86-64 binary compiled from L2,
talking to the kernel through raw syscalls: no libc, no runtime.</p>
<ul>
<li><a href=\"/hello\">/hello</a> plain text</li>
<li><a href=\"/stats\">/stats</a> request counter</li>
<li><a href=\"/echo\">/echo</a> your request, sent back</li>
<li><a href=\"/shutdown\">/shutdown</a> stop the server</li>
</ul>
</body></html>
"
end

word route
    "/" path= if
        "200 OK" "text/html; charset=utf-8" index_page respond
    else "/hello" path= if
        "200 OK" "text/plain" "Hello from L2!\n" respond
    else "/stats" path= if
        out_reset
        "requests served: " out_str served @ out_num "\n" out_str
        "200 OK" "text/plain" out_buf out_len @ respond
    else "/echo" path= if
        "200 OK" "text/plain" req_buf req_len @ respond
    else "/shutdown" path= if
        "200 OK" "text/plain" "bye\n" respond
        head_only @ 0 == if running 0 ! end     # HEAD changes nothing
    else
        "404 Not Found" "text/plain" "not found\n" respond
    end
end

# ---------------------------------------------------------------------------
#  Request handling
# ---------------------------------------------------------------------------

#log_request [*, method_addr | method_len] -> [*]
word log_request
    write_buf " " write_buf path_addr @ path_len @ puts
end

#handle_client [*] -> [*]
# Reads one request, parses its request line and answers it.
word handle_client
    head_only 0 !
    client_fd @ req_buf REQ_CAP syscall.read
    dup 0 <= if drop else
        req_len swap !
        served served @ 1 + !
        req_buf req_len @ CH_SPACE indexof          # end of the method
        dup 0 <= if
            drop "400 Bad Request" "text/plain" "bad request\n" respond
        else
            # The path runs from after the first space to the next space.
            dup 1 + req_buf + path_addr swap !
            req_len @ over - 1 - path_len swap !
            path_addr @ path_len @ CH_SPACE indexof
            dup 0 >= if path_len swap ! else drop end
            # Drop a query string.
            path_addr @ path_len @ CH_QUESTION indexof
            dup 0 >= if path_len swap ! else drop end
            req_buf swap                            # method_addr method_len
            2dup log_request
            2dup "GET" strcmp if
                2drop route
            else "HEAD" strcmp if
                head_only 1 ! route
            else
                method_not_allowed
            end
        end
    end
end

# ---------------------------------------------------------------------------
#  Socket setup
# ---------------------------------------------------------------------------

#fail [*, addr | len] -> never
word fail
    "error: " ewrite_buf eputs
    1 exit
end

word parse_port
    argc 2 >= if
        1 argv@ dup strlen toint? drop          # value is 0 when invalid
    else
        DEFAULT_PORT
    end
    dup 1 < over 65535 > or if drop "invalid port (expected 1-65535)" fail end
    port swap !
end

# struct sockaddr_in for 127.0.0.1:<port>, written as two little-endian qwords:
# family (2 bytes), port in network byte order (2 bytes), address (4 bytes), padding.
word fill_sockaddr
    sockaddr
    AF_INET
    port @ 8 shr 255 band 16 shl bor
    port @ 255 band 24 shl bor
    127 32 shl bor
    1 56 shl bor
    !
    sockaddr 8 + 0 !
end

word open_server
    AF_INET SOCK_STREAM 0 syscall.socket
    dup 0 < if drop "socket() failed" fail end
    server_fd swap !

    optval 1 !
    server_fd @ SOL_SOCKET SO_REUSEADDR optval 4 syscall.setsockopt
    0 < if "setsockopt() failed" fail end

    fill_sockaddr
    server_fd @ sockaddr 16 syscall.bind
    0 < if "bind() failed (port already in use?)" fail end

    server_fd @ BACKLOG syscall.listen
    0 < if "listen() failed" fail end
end

word main
    parse_port
    open_server
    "listening on http://127.0.0.1:" write_buf port @ puti cr
    running 1 !
    while running @ do
        server_fd @ 0 0 syscall.accept
        dup 0 < if drop else
            client_fd swap !
            handle_client
            client_fd @ syscall.close drop
        end
    end
    server_fd @ syscall.close drop
    "served " write_buf served @ putu " requests, shutting down\n" write_buf
    0
end
