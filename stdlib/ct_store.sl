# Compile-time key/value store shared by every macro and compile-time word.
#
#   import ct_store.sl
#   macro remember (x) "last" $x ct-store-set ;
#   macro recall () `$( "last" ct-store-get drop )` emit ;
#
# Keys are strings. Values can be numbers, strings, nil, lists, maps, Tokens,
# and Syntax; handles (definition, event subscription, rewrite) cannot be
# stored. The store lives for one compilation. A read
# returns the stored value itself, so a list or map read from the store and
# then changed is changed in the store too; store a clone to avoid that.
#
# The values are kept in the metadata map of a private macro, which macro
# metadata is designed to hold.

macro __ct-store-anchor () ;

# [*] -> [* | map]
word __ct-store-map
	"__ct-store-anchor" ct-macro-get-by-name
	0 == if "ct_store.sl: the store's anchor macro is missing" ct-parser-error end
	"metadata" map-get drop
end
compile-only

# [* | map] -> [*]
word __ct-store-save
	map-new swap "metadata" swap map-set
	"__ct-store-anchor" swap ct-macro-set-by-name
	0 == if "ct_store.sl: could not update the store" ct-parser-error end
end
compile-only

# Store a value under a key, replacing any earlier value.
# [*, key | value] -> [*]
word ct-store-set
	__ct-store-map rot rot map-set __ct-store-save
end
compile-only

# The value stored under a key and 1, or nil and 0.
# [* | key] -> [*, value | found]
word ct-store-get
	__ct-store-map swap map-get
end
compile-only

# The value stored under a key, or the given default.
# [*, key | default] -> [* | value]
word ct-store-get-or
	swap ct-store-get if nip else drop end
end
compile-only

# Whether a key is stored.
# [* | key] -> [* | flag]
word ct-store-has?
	__ct-store-map swap map-has?
end
compile-only

# Remove a key (nothing happens when it is absent).
# [* | key] -> [*]
word ct-store-delete
	__ct-store-map swap map-delete drop __ct-store-save
end
compile-only

# The stored keys, sorted.
# [*] -> [* | keys]
word ct-store-keys
	__ct-store-map map-keys
end
compile-only

# Remove every key.
# [*] -> [*]
word ct-store-clear
	map-new __ct-store-save
end
compile-only
