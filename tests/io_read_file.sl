import ../stdlib/stdlib.sl
import ../stdlib/io.sl

word main
    "/tmp/l2_read_file_test.txt"
    "read_file works\n"
    write_file drop

    "/tmp/l2_read_file_test.txt" with path path_len in
        path path_len read_file_size
        dup 0 < if
            "read_file_size failed: " puts
            puti
            1 exit
        end
        with size in
            size alloc with buffer in
                path path_len buffer size read_file
                dup 0 < if
                    "read_file failed: " puts
                    puti
                    1 exit
                end
                buffer swap write_buf

                path path_len buffer size read_file2
                dup 0 < if
                    "read_file2 failed: " puts
                    puti
                    1 exit
                end
                buffer swap write_buf

                "/tmp/l2_missing_io_file_test.txt" with missing missing_len in
                    missing missing_len read_file_size
                    dup 0 < if
                        drop
                    else
                        drop 1 exit
                    end
                    missing missing_len buffer size read_file
                    dup 0 < if
                        drop
                    else
                        drop 1 exit
                    end
                    missing missing_len buffer size read_file2
                    dup 0 < if
                        drop
                    else
                        drop 1 exit
                    end
                end

                0
                buffer size free
                exit
            end
        end
    end
end
