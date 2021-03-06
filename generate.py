#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  Download and parse FHIR resource definitions
#  Supply "-f" to force a redownload of the spec

import io
import sys
import os.path
import shutil
import glob
import re
import json
import datetime
from jinja2 import Environment, PackageLoader
from jinja2.filters import environmentfilter

from settings import *


cache = 'downloads'
loglevel = 0

skip_properties = [
    'extension',
    'modifierExtension',
    'language',
    'contained',
]

jinjaenv = Environment(loader=PackageLoader('generate', '.'))


def log0(*logstring):
    if loglevel >= 0:
        print(' '.join(str(s) for s in logstring))

def log1(*logstring):
    if loglevel > 0:
        print(' '.join(str(s) for s in logstring))


def download(url, path):
    """ Download the given URL to the given path.
    """
    import requests     # import here as we can bypass its use with a manual download
    
    log0('->  Downloading {}'.format(url))
    ret = requests.get(url)
    assert(ret.ok)
    with io.open(path, 'wb') as handle:
        for chunk in ret.iter_content():
            handle.write(chunk)


def expand(path, target):
    """ Expand the ZIP file at the given path to the given target directory.
    """
    assert(os.path.exists(path))
    import zipfile      # import here as we can bypass its use with a manual unzip
    
    log0('->  Extracting to {}'.format(target))
    with zipfile.ZipFile(path) as z:
        z.extractall(target)


def parse(path):
    """ Parse all JSON profile definitions found in the given expanded
    directory, create classes for all found profiles, collect all search params
    and generate the search param extension.
    """
    assert(os.path.exists(path))
    
    # get FHIR version
    version = None
    with io.open(os.path.join(path, 'version.info'), 'r', encoding='utf-8') as handle:
        text = handle.read()
        for line in text.split("\n"):
            if '=' in line:
                (n, v) = line.split('=', 2)
                if 'FhirVersion' == n:
                    version = v
    
    assert(version is not None)
    log0("->  This is FHIR version {}".format(version))
    now = datetime.date.today()
    info = {
        'version': version.strip() if version else 'X',
        'date': now.isoformat(),
        'year': now.year
    }
    
    # parse profiles
    all_classes = {}
    factories = set()
    search_params = set()
    in_profiles = {}
    for prof in glob.glob(os.path.join(path, '*.profile.json')):
        profile_name, classes, srch_prms, supp_profs = process_profile(prof, info)
        
        if profile_name is not None:
            factories.add(profile_name)
            for klass in classes:
                cn = klass.get('className')
                assert(cn is not None)
                if cn in all_classes:
                    log1("xxx>  Already have class {}".format(cn))
                else:
                    all_classes[cn] = klass
        
        if srch_prms is not None:
            search_params |= srch_prms
            for spp in supp_profs:
                if spp in in_profiles:
                    in_profiles[spp].add(profile_name)
                else:
                    in_profiles[spp] = set([profile_name])
    
    # write base classes
    if write_resources and len(all_classes) > 0:
        for base in resource_baseclasses:
            if os.path.exists(base):
                tgt = os.path.join(resource_base_target, os.path.basename(base))
                log0("-->  Copying base class {} to {}".format(os.path.basename(base), tgt))
                shutil.copyfile(base, tgt)
    
    # process element factory
    process_factories(factories, info)
    
    # process search parameters
    process_search(search_params, in_profiles, info)
    
    # detect and process unit tests
    process_unittests(path, all_classes, info)


def process_profile(path, info):
    """ Parse one profile file, render the class and return possible search
    parameters.
    
    :returns: A tuple with (profile-name, [found classes], "name|original-name|type", search-
        param-list)
    """
    assert(os.path.exists(path))
    
    # read the profile
    profile = None
    with io.open(path, 'r', encoding='utf-8') as handle:
        profile = json.load(handle)
    
    assert(profile != None)
    assert('Profile' == profile['resourceType'])
    
    structure_arr = profile.get('structure')
    if structure_arr is None or 0 == len(structure_arr):
        log0('xx>  Profile {} has no structure'.format(path))
        return None, None, None, None
    
    info['filename'] = filename = os.path.basename(path)
    requirements = profile.get('requirements')
    structure = structure_arr[0]
    
    # figure out which type/class this is
    
    # Some profiles, such as "Age", basically define a subclass of a type, like
    # "Quantity", which is not apparent from inside the `element` definitions.
    # OTOH, "LipidProfile" on "DiagnosticReport" extends a profile - we are
    #
    # NOT YET
    #
    # handling these - well we are dumping these as well, but not handling any
    # additional attributes. Not yet sure if that's correct and we're not
    # adding these classes to the repo
    is_subclass = False
    superclass = structure['type']
    main = structure.get('name')
    if main is None:
        main = superclass
    elif main != superclass:
        is_subclass = True
    info['main'] = main
    
    log0('-->  Parsing profile {}  --  {}'.format(main, filename))
    classes = []
    
    # loop elements
    mapping = {}
    if 'snapshot' in structure:
        elements = structure['snapshot'].get('element', [])     # 0.3 (or nightly)
    else:
        elements = structure.get('element', [])                 # 0.28
    
    for element in elements:
        elem_path = element['path']
        parts = elem_path.split('.')
        classpath = '.'.join(parts[:-1]) if len(parts) > 1 else parts[0]
        name = parts[-1]
        
        if name in skip_properties:
            log1('--->  Skipping {} property'.format(name))
            continue
        
        definition = element.get('definition')
        if definition is None:
            log0('xx>  No definition for {}'.format(elem_path))
            continue
        
        k = mapping.get(classpath)
        newklass = parse_elem(elem_path, name, definition, k)
        
        # element describes a new class
        if newklass is not None:
            mapping[newklass['path']] = newklass
            classes.append(newklass)
            
            # is this the resource description itself?
            if elem_path == main:
                newklass['resourceName'] = main
                newklass['formal'] = requirements
            
            # this is a "subclass", such as "Age" on "Quantity"
            elif is_subclass:
                log1('--->  Treating {} as subclass of {}'.format(main, superclass))
                newklass['className'] = main
                newklass['superclass'] = superclass
                newklass['is_subclass'] = True
                newklass['short'] = profile.get('name')
                newklass['formal'] = profile.get('description')
                break
    
    # determine imported classes
    inline = set()
    names = set()
    imports = []
    for klass in classes:
        inline.add(klass['className'])
    
    for klass in classes:
        sup = klass.get('superclass')
        if sup is not None and sup not in names:
            names.add(sup)
            if sup not in natives and sup not in inline:
                imports.append(sup)
        
        for prop in klass['properties']:
            name = prop['className']
            if name not in names:
                names.add(name)
                if name not in natives and name not in inline:
                    imports.append(name)
            
            refTo = prop.get('isReferenceTo')
            if refTo is not None and refTo not in names:
                names.add(refTo)
                if refTo not in natives and refTo not in inline:
                    imports.append(refTo)
    
    info['imports'] = sorted(imports)
    info['lowercase_import_hack'] = ptrn_filenames_lowercase
    
    if write_resources:
        ptrn = main.lower() if ptrn_filenames_lowercase else main
        render({'info': info, 'classes': classes}, tpl_resource_source, tpl_resource_target_ptrn.format(ptrn))
    
    # get search params
    search_params = set()
    supported = set()
    params = structure.get('searchParam', [])   # list of dictionaries with "name", "type" and "documentation"
    for param in params:
        name = param['name']
        tp = param['type']
        if name and tp:
            orig = name
            name = re.sub(r'[^\w\d\-]', '', name)
            if '-' in name:
                if search_generate_camelcase:
                    name = _camelCase(name, '-')
                else:
                    name = name.replace('-', '_')
            
            search_params.add('{}|{}|{}'.format(name, orig, tp))
            supported.add(name)
    
    return main, classes, search_params, supported


def parse_elem(path, name, definition, klass):
    """ Parse one profile element (which will become a class property).
    A `klass` dictionary may be passed in, in which case the element's
    definitions will be interpreted in its context. A new class may be returned
    if an inline defined subtype is detected.
    
    :param path: The path to the element, like "MedicationPrescription.identifier"
    :param name: The name of the property, like "identifier"
    :param definition: The element's definition
    :param klass: The owning class of the element, if it has just been parsed
    :returns: A dictionary with class attributes, if and only if an inline-
        defined subtype is detected
    """
    short = definition['short']
    formal = definition['formal']
    if formal and short == formal[:-1]:     # formal adds a trailing period
        formal = None
    n_min = definition['min']
    n_max = definition['max']
    
    # determine property class(es)
    types = []
    haz = set()
    for tp in definition.get('type', []):
        code = tp['code']
        if code not in haz:
            haz.add(code)
            types.append((code, tp.get('profile', None)))
    
    # no type means this is an inline-defined subtype, create a class for it
    newklass = None
    if klass is None or 0 == len(types):
        className = ''.join(['{}{}'.format(s[:1].upper(), s[1:]) for s in path.split('.')])
        newklass = {
            'path': path,
            'className': className,
            'superclass': classmap.get(types[0][0], resource_default_base) if len(types) > 0 else resource_default_base,
            'short': short,
            'formal': formal,
            'properties': [],
            'hasNonoptional': False,
        }
        
        if 0 == len(types):
            types.append((className, None))
    
    # add as properties to class
    if klass is not None:
        for tp, ref in types:
            process_elem_type(klass, name, tp, ref, short, formal, n_min, n_max)
        
        # sort properties by name
        if len(klass['properties']) > 0:
            klass['properties'] = sorted(klass['properties'], key=lambda x: x['name'])
    
    return newklass

def process_elem_type(klass, name, tp, ref, short, formal, n_min, n_max):
    """ Handle one element (property) type and return a dict describing the property.
    """
    
    # The wildcard type, expand to all possible types, as defined in our mapping
    if '*' == tp:
        for exp_type in starexpandtypes:
            process_elem_type(klass, name, exp_type, ref, short, formal, n_min, n_max)
        return
    
    if '[x]' in name:
        # TODO: "MedicationPrescription.reason[x]" can be a
        # "ResourceReference" but apparently should be called
        # "reasonResource", NOT "reasonResourceReference". Interesting.
        kl = 'Resource' if 'ResourceReference' == tp else tp
        name = name.replace('[x]', '{}{}'.format(kl[:1].upper(), kl[1:]))
    
    # reference?
    if ref is not None:
        ref = ref.replace('http://hl7.org/fhir/profiles/', '')      # could be cleaner
        ref = classmap.get(ref, ref)
    
    # describe the property
    mappedClass = classmap.get(tp, tp)
    prop = {
        'name': reservedmap.get(name, name),
        'orig_name': name,
        'short': short,
        'className': mappedClass,
        'jsonClass': jsonmap.get(mappedClass, jsonmap_default),
        'isArray': True if '*' == n_max else False,
        'isReferenceTo': ref,
        'nonoptional': 0 != int(n_min),
        'isNative': True if mappedClass in natives else False,
    }
    
    klass['properties'].append(prop)
    if prop['nonoptional']:
        klass['hasNonoptional'] = True


def process_factories(factories, info):
    """ Renders a template which creates an extension to FHIRElement that has
    a factory method with all FHIR resource types.
    """
    if not write_factory:
        log1("oo>  Skipping factory")
        return
    
    data = {
        'info': info,
        'classes': factories,
    }
    render(data, tpl_factory_source, tpl_factory_target)


def process_search(params, in_profiles, info):
    """ Processes and renders the FHIR search params extension.
    """
    if not write_searchparams:
        log1("oo>  Skipping search parameters")
        return
    
    extensions = []
    dupes = set()
    for param in sorted(params):
        (name, orig, typ) = param.split('|')
        finalname = reservedmap.get(name, name)
        for d in extensions:
            if finalname == d['name']:
                dupes.add(finalname)
        
        extensions.append({'name': finalname, 'original': orig, 'type': typ})
    
    data = {
        'info': info,
        'extensions': extensions,
        'in_profiles': in_profiles,
        'dupes': dupes,
    }
    render(data, tpl_searchparams_source, tpl_searchparams_target)


def process_unittests(path, classes, info):
    """ Finds all example JSON files and uses them for unit test generation.
    Test files use the template `tpl_unittest_source` and dump it according to
    `tpl_unittest_target_ptrn`.
    """
    all_tests = {}
    for utest in glob.glob(os.path.join(path, '*-example*.json')):
        log0('-->  Parsing unit test {}'.format(os.path.basename(utest)))
        class_name, tests = process_unittest(utest, classes)
        if class_name is not None:
            test = {
                'filename': os.path.join(unittest_filename_prefix, os.path.basename(utest)),
                'tests': tests,
            }
            
            if class_name in all_tests:
                all_tests[class_name].append(test)
            else:
                all_tests[class_name] = [test]
    
    if write_unittests:
        for klass, tests in all_tests.items():
            data = {
                'info': info,
                'class': klass,
                'tests': tests,
            }
            ptrn = klass.lower() if ptrn_filenames_lowercase else klass
            render(data, tpl_unittest_source, tpl_unittest_target_ptrn.format(ptrn))
        
        # copy unit test files, if any
        if unittest_copyfiles is not None:
            for utfile in unittest_copyfiles:
                if os.path.exists(utfile):
                    tgt = os.path.join(unittest_copyfiles_base, os.path.basename(utfile))
                    log0("-->  Copying unittest file {} to {}".format(os.path.basename(utfile), tgt))
                    shutil.copyfile(utfile, tgt)
    else:
        log1('oo>  Not writing unit tests')


def process_unittest(path, classes):
    """ Process a unit test file at the given path, determining class structure
    from the given classes dict.
    
    :returns: A tuple with (top-class-name, [test-dictionaries])
    """
    utest = None
    assert(os.path.exists(path))
    with io.open(path, 'r', encoding='utf-8') as handle:
        utest = json.load(handle)
    assert(utest != None)
    
    # find the class
    className = utest.get('resourceType')
    assert(className != None)
    del utest['resourceType']
    klass = classes.get(className)
    if klass is None:
        log0('xx>  There is no class for "{}"'.format(className))
        return None, None
    
    # TODO: some "subclasses" like Age are empty because all their definitons are in their parent (Quantity). This
    # means that later on, the property lookup fails to find the properties for "Age", so fix this please.
    
    # gather testable properties
    tests = process_unittest_properties(utest, klass, classes)
    return className, sorted(tests, key=lambda x: x['path'])


def process_unittest_properties(utest, klass, classes, prefix=None):
    """ Process one level of unit test properties interpreted for the given
    class.
    """
    assert(klass != None)
    
    props = {}
    for cp in klass.get('properties', []):      # could cache this, but... lazy
        props[cp['name']] = cp
    
    # loop item's properties
    tests = []
    for key, val in utest.items():
        prop = props.get(key)
        if prop is None:
            log1('xxx>  Unknown property "{}" in unit test on {}'.format(key, klass.get('className')))
        else:
            propClass = prop['className']
            path = unittest_format_path_key.format(prefix, key) if prefix else key
            
            # property is an array
            if list == type(val):
                i = 0
                for v in val:
                    mypath = unittest_format_path_index.format(path, i)
                    tests.extend(handle_unittest_property(mypath, v, propClass, classes))
                    i += 1
            else:
                tests.extend(handle_unittest_property(unittest_format_path_prepare.format(path), val, propClass, classes))
    
    return tests


def handle_unittest_property(path, value, klass, classes):
    assert(path is not None)
    assert(value is not None)
    assert(klass is not None)
    tests = []
    
    # property is another element, recurse
    if dict == type(value):
        subklass = classes.get(subclassmap[klass] if klass in subclassmap else klass)
        if subklass is None:
            log1('xxx>  No class {} found for "{}"'.format(klass, path))
        else:
            tests.extend(process_unittest_properties(value, subklass, classes, path))
    else:
        isstr = isinstance(value, str)
        if not isstr and sys.version_info[0] < 3:       # Python 2.x has 'str' and 'unicode'
            isstr = isinstance(value, basestring)
            
        tests.append({'path': path, 'class': klass, 'value': value.replace("\n", "\\n") if isstr else value})
    
    return tests


def render(data, template, filepath):
    """ Render the given class data using the given Jinja2 template, writing
    the output into 'Models'.
    """
    assert(os.path.exists(template))
    template = jinjaenv.get_template(template)
    
    if not filepath:
        raise Exception("No target filepath provided")
    dirpath = os.path.dirname(filepath)
    if not os.path.isdir(dirpath):
        os.makedirs(dirpath)
    
    with io.open(filepath, 'w', encoding='utf-8') as handle:
        log0('-->  Writing {}'.format(filepath))
        rendered = template.render(data)
        handle.write(rendered)
        # handle.write(rendered.encode('utf-8'))


def _camelCase(string, splitter='_'):
    """ Turns a string into CamelCase form without changing the first part's
    case.
    """
    if not string:
        return None
    
    name = ''
    i = 0
    for n in string.split(splitter):
        if i > 0:
            name += n[0].upper() + n[1:]
        else:
            name = n
        i += 1
    
    return name

# There is a bug in Jinja's wordwrap (inherited from `textwrap`) in that it
# ignores existing linebreaks when applying the wrap:
# https://github.com/mitsuhiko/jinja2/issues/175
# Here's the workaround:
@environmentfilter
def do_wordwrap(environment, s, width=79, break_long_words=True, wrapstring=None):
    """
    Return a copy of the string passed to the filter wrapped after
    ``79`` characters.  You can override this default using the first
    parameter.  If you set the second parameter to `false` Jinja will not
    split words apart if they are longer than `width`.
    """
    import textwrap
    if not wrapstring:
        wrapstring = environment.newline_sequence
    
    accumulator = []
    # Workaround: pre-split the string
    for component in re.split(r"\r?\n", s):
        # textwrap will eat empty strings for breakfirst. Therefore we route them around it.
        if len(component) is 0:
            accumulator.append(component)
            continue
        accumulator.extend(
            textwrap.wrap(component, width=width, expand_tabs=False,
                replace_whitespace=False,
                break_long_words=break_long_words)
        )
    return wrapstring.join(accumulator)

jinjaenv.filters['wordwrap'] = do_wordwrap


if '__main__' == __name__:
    
    # start from scratch?
    if len(sys.argv) > 1 and '-f' == sys.argv[1]:
        if os.path.isdir(cache):
            shutil.rmtree(cache)
    else:
        log0('->  Using cached FHIR spec, supply "-f" to re-download')
    
    # download spec if needed and extract
    path_spec = os.path.join(cache, os.path.split(specification_url)[1])
    expanded_spec = os.path.dirname(path_spec)

    if not os.path.exists(path_spec):
        if not os.path.isdir(cache):
            os.mkdir(cache)
        download(specification_url, path_spec)
        expand(path_spec, expanded_spec)

    # parse
    parse(os.path.join(expanded_spec, 'site'))

