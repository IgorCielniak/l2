import stdlib.sl

# A :py body is raw Python: single-quoted and triple-quoted strings, braces
# in dict literals and f-strings, and '}' inside strings or comments do not
# end it early.
:py pyword {
def macro(ctx):
    table = {'a': 1, 'bc': 2}   # a } in a comment
    doc = '''multi 'quoted' } text'''
    label = f"{table['bc']}"
    assert label == "2" and doc.startswith('multi')
    return None
};

word main
  pyword
  "py ok" puts
end
