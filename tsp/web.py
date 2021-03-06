"""Module containing webservice to interact with TSP library."""
import json
import logging
import os
import falcon
import numpy as np
from redis import StrictRedis
from choke import RedisChokeManager, CallLimitExceededError
from tsp.solver import sample_from_distance_matrix
from dwave.system.samplers import DWaveSampler
from dwave.system.composites import FixedEmbeddingComposite
from dwave.system.composites import EmbeddingComposite
import gc
import math

# unitary:web
BASIC_AUTH_TOKEN = 'Basic dW5pdGFyeTp3ZWI='
STATIC_DIRECTORY = os.path.abspath(os.path.join(os.getcwd(), './build'))
INDEX_HTML = os.path.abspath(os.path.join(STATIC_DIRECTORY, './index.html'))
REDIS = StrictRedis(
    host=os.getenv('REDIS_HOST', 'localhost'),
    port=int(os.getenv('REDIS_PORT', '6379')),
    password=os.getenv('REDIS_PASSWORD', None))

CHOKE_MANAGER = RedisChokeManager(REDIS)
LOGGER_NAME = 'tsp.api'

class AuthMiddleware(object):

    def process_request(self, req, resp):
        token = req.get_header('Authorization')
        if token is None or token != BASIC_AUTH_TOKEN:
            raise falcon.HTTPUnauthorized('Authentication required', headers=[('WWW-Authenticate', 'Basic realm=Authorization Required')])



logging.basicConfig(level='WARNING')
logging.getLogger('redis_choke').setLevel('DEBUG')

DWAVE_ENDPOINT = 'https://cloud.dwavesys.com/sapi'
DWAVE_TOKEN = os.getenv('DWAVE_TOKEN', None)

if DWAVE_TOKEN is None:
    logging.getLogger(LOGGER_NAME).warning('D-Wave token not configured. Only local requests will be supported.')


class TSPResource(object):
    """Resource for computing TSP solution."""
    def __init__(self):
        self.solvers_list = {}
        sampler = DWaveSampler(token=DWAVE_TOKEN, endpoint=DWAVE_ENDPOINT)
        for i in range(4, 10):
            embedding = np.load(os.path.join('embeddings', 'embedding_' + str(i) + '.npy')).item()
            for key in embedding.keys():
                embedding[key] = [int(item) for item in embedding[key]]
            try:
                self.solvers_list[i] = FixedEmbeddingComposite(sampler, embedding=embedding)
            except Exception as e:
                pass
        self.backup_solver = EmbeddingComposite(sampler)

    @staticmethod
    @CHOKE_MANAGER.choke(
        window_length=float(os.getenv('CHOKE_WINDOW_LENGTH')),
        limit=float(os.getenv('CHOKE_LIMIT')))
    def solve_using_dwave(dist_matrix, dist_mul, const_mul, start, end, solver):
        """Solve TSP problem using D-Wave."""
        logging.getLogger(LOGGER_NAME).info(
            'Calling sample_from_distance_matrix with args: '
            '%s %s %s %s %s %s %s %s',
            dist_matrix, dist_mul, const_mul, start, end, True, solver, DWAVE_TOKEN)
        return sample_from_distance_matrix(
            dist_matrix,
            dist_mul,
            const_mul,
            start=start,
            end=end,
            use_dwave=True,
            dwave_token=DWAVE_TOKEN,
            solver=solver)

    @staticmethod
    def solve_clasically(dist_matrix, dist_mul, const_mul, start, end):
        """Solve TSP using classical emulator."""
        return sample_from_distance_matrix(
            dist_matrix,
            dist_mul,
            const_mul,
            start=start,
            end=end)

    def on_post(self, req, resp):
        """The POST handler."""
        payload = json.load(req.bounded_stream)

        start = payload.get('start_node', None)
        end = payload.get('end_node', start)
        use_dwave = payload.get('use_dwave', False)

        if use_dwave and DWAVE_TOKEN is None: # Terminate early if D-Wave solution requested
            use_dwave = False


        try:
            dist_matrix = np.array(payload['distances'], dtype='float64')
        except KeyError:
            msg = 'The "distances" matrix is absent from the request.'
            raise falcon.HTTPBadRequest('Bad request', msg)
        except ValueError:
            msg = 'The "distances" field should be a correct matrix.'
            raise falcon.HTTPBadRequest('Bad request', msg)

        if len(dist_matrix.shape) != 2 or dist_matrix.shape[0] != dist_matrix.shape[1]:
            raise falcon.HTTPBadRequest('Bad request', 'The "distances" matrix should be square.')

        dist_mul = payload.get('dist_mul', 10)
        const_mul = payload.get('const_mul', 400)

        # Flag indicating whether we will need to solve classically.
        # Obviously is we dont use D-Wave this should be true already.
        classical_solution_needed = not use_dwave
        import os
        import psutil
        process = psutil.Process(os.getpid())
        print("MEMORY BEFORE:", convert_size(process.memory_info().rss))

        if use_dwave:
            try:
                try:
                    solver = self.solvers_list[int(dist_matrix.shape[0])]
                except Exception as e:
                    print(e)
                    print("Problem with solver, using backup solver")
                    solver = self.backup_solver
                result = self.solve_using_dwave(
                    dist_matrix,
                    dist_mul,
                    const_mul,
                    start=start,
                    end=end,
                    solver=solver)
                if -1 in result.route:
                    print("D-Wave unable to find proper solution")
                    classical_solution_needed = True
            except CallLimitExceededError:
                logger = logging.getLogger('tsp.api')
                logger.warning('Throttling triggered. Classical solution will be returned')
                classical_solution_needed = True
            except Exception as e:
                print("Unexpected error:", e)
                classical_solution_needed = True

        if classical_solution_needed:
            result = self.solve_clasically(dist_matrix, dist_mul, const_mul, start=start, end=end)
        print("MEMORY AFTER:", convert_size(process.memory_info().rss))

        resp.content_type = falcon.MEDIA_JSON
        resp.body = json.dumps({
            'route': result.route,
            'distance': result.mileage,
            'energy': result.energy,
            'info': result.info,
        })
        gc.collect()

# api = falcon.API(middleware=[
#                      AuthMiddleware()
#                  ])
api = falcon.API()

def index_html_sink(req, resp):
    resp.content_type = 'text/html; charset=utf-8'

    with open(INDEX_HTML, 'rt') as f:
        resp.body = f.read()

api.add_sink(index_html_sink, prefix='^/$')
api.add_static_route('/', STATIC_DIRECTORY)
api.add_route('/tsp/solve', TSPResource())


def convert_size(size_bytes):
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return "%s %s" % (s, size_name[i])
