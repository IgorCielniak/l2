import stdlib/stdlib.sl
import "import_helpers/space helper.sl" word after_quoted_import 900 puti cr end
import import_helpers/plain_import.sl word after_unquoted_import 901 puti cr end

word main
    after_quoted_import
    after_unquoted_import
    from_space_helper
    from_plain_helper
    0
end
